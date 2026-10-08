"""Health check: `stories.py doctor` and `install.py --check`."""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys

from . import __version__, config, db, notify, pace

UNIT = "telegram-stories.service"


def _line(ok, label: str, detail: str = "") -> str:
    mark = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
    return f"{mark} {label}" + (f" — {detail}" if detail else "")


def service_state() -> str:
    if not shutil.which("systemctl"):
        return "no-systemd"
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def rich_messages_enabled() -> bool | None:
    path = config.hermes_home() / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return bool(re.search(r"(?m)^\s*rich_messages:\s*(true|yes|on)\s*$", text, re.IGNORECASE))


def status_text(con, cfg) -> str:
    ar = cfg.get("autoresponder", {})
    rules = dict(con.execute("SELECT status, COUNT(*) FROM rules GROUP BY status").fetchall())
    day = con.execute("SELECT COUNT(*) FROM deliveries WHERE status='sent' AND reason IS NULL AND sent_at>?",
                      (db.now() - 86400,)).fetchone()[0]
    kill = str(db.get_state(con, "kill_switch", "0")) == "1"
    breaker = int(db.get_state(con, "breaker_until", "0") or 0)
    lines = [f"autoresponder: {'ENABLED' if ar.get('enabled') else 'disabled'} in config"
             + (" · STOPPED (kill switch)" if kill else "")
             + (" · paused by Telegram flood limit" if breaker > db.now() else ""),
             "rules: " + (", ".join(f"{k} {v}" for k, v in sorted(rules.items())) or "none"),
             "pace: " + pace.describe(con, cfg),
             f"automatic messages sent in 24 h: {day}"]
    return "\n".join(lines)


def run(cfg: dict, online: bool = True) -> int:
    out, bad = [], 0
    out.append(f"telegram-stories {__version__} · python {sys.version.split()[0]} ({sys.executable})")
    cpath = config.config_path()
    out.append(_line(cpath.exists() or None, "config", str(cpath) if cpath.exists() else f"{cpath} missing — defaults"))
    try:
        con = db.connect(create=False)
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        out.append(_line(True, "database", f"{config.db_path()} (schema v{ver})"))
    except SystemExit as exc:
        out.append(_line(False, "database", str(exc)))
        print("\n".join(out))
        return 1
    session = config.secret(cfg.get("session_env") or "STORIES_SESSION_STRING")
    out.append(_line(bool(session), "session", "present in env" if session else "missing — run `stories.py login`"))
    bad += 0 if session else 1
    try:
        import telethon
        from telethon.tl.alltlobjects import LAYER

        from .tg import MIN_LAYER
        fresh = LAYER >= MIN_LAYER
        out.append(_line(True if fresh else None, "telethon", f"{telethon.__version__} (layer {LAYER})" + (
            "" if fresh else f" — older than what Telegram sends (layer {MIN_LAYER}): the service can lose the "
                             "connection; update: pip install -U telethon")))
    except ImportError:
        py = db.get_meta(con, "python")
        out.append(_line(None, "telethon", f"not importable here; service python: {py or 'unknown'}"))
    owner = db.owner_id(con)
    prem = db.get_meta(con, "owner_premium")
    if owner:
        out.append(_line(True, "owner", f"id {owner}, premium: {'yes' if prem == '1' else 'no'}"))
        if prem != "1":
            out.append(_line(None, "history", "without Premium, viewer lists vanish 24 h after a story expires"))
    st = service_state()
    out.append(_line(st == "active" or (None if st in ("no-systemd", "unknown") else False), "service",
                     f"{UNIT}: {st}"))
    last = int(db.get_state(con, "last_poll_at", "0") or 0)
    age = db.now() - last if last else None
    if age is None:
        out.append(_line(None, "last poll", "never"))
    else:
        out.append(_line(pace.poll_age_ok(con, cfg, age) or None, "last poll",
                         f"{age // 60} min ago ({pace.describe(con, cfg)})"))
    n_st = con.execute("SELECT COUNT(*), SUM(backfilled_at IS NOT NULL), SUM(list_available=1) FROM stories "
                       "WHERE peer_id=?", (owner or 0,)).fetchone()
    n_views = con.execute("SELECT COUNT(*) FROM views").fetchone()[0]
    n_people = con.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    out.append(_line(True, "data", f"{n_st[0] or 0} stories ({n_st[1] or 0} imported, {n_st[2] or 0} with viewer "
                                   f"lists), {n_views} viewer rows, {n_people} people"))
    for ch in cfg.get("channels") or []:
        row = con.execute("SELECT p.peer_id, COUNT(s.story_id) FROM peers p LEFT JOIN stories s ON s.peer_id=p.peer_id "
                          "WHERE lower(p.username)=lower(?) GROUP BY p.peer_id", (ch.lstrip("@"),)).fetchone()
        out.append(_line(True if row else None, f"channel {ch}", f"{row[1]} stories" if row else "not imported yet"))
    out.append(_line(True, "notifications", notify.mode(cfg)))
    rich = rich_messages_enabled()
    if rich is False:
        out.append(_line(None, "tables in Telegram", "Hermes rich_messages is off — tables arrive as bullet lists; "
                                                     "set platforms.telegram.extra.rich_messages: true"))
    elif rich:
        out.append(_line(True, "tables in Telegram", "rich_messages on"))
    out.append(status_text(con, cfg))
    if online and session:
        try:
            from . import tg

            async def _probe():
                client = await tg.connect(cfg)
                try:
                    me = await client.get_me()
                    return me
                finally:
                    await client.disconnect()
            me = asyncio.run(_probe())
            out.append(_line(True, "telegram", f"session authorized as id {me.id}"))
        except SystemExit as exc:
            out.append(_line(False, "telegram", str(exc)))
            bad += 1
        except Exception as exc:  # noqa: BLE001
            out.append(_line(False, "telegram", f"{type(exc).__name__}: {exc}"))
            bad += 1
    print("\n".join(out))
    return 1 if bad else 0
