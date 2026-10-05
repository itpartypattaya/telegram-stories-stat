"""Paths, configuration and secrets.

Layout (all overridable with environment variables):
  $HERMES_HOME                  ~/.hermes by default
  $STORIES_CONFIG               $HERMES_HOME/telegram-stories.json
  $STORIES_HOME                 $HERMES_HOME/data/telegram-stories   (database, thumbnails, exports)
  secrets                       process environment first, then $HERMES_HOME/.env

The session string is a full-access credential for the Telegram account. It is
read from the environment / .env only and is never written to the config, the
database or the logs.
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from datetime import timezone, tzinfo
from pathlib import Path

DEFAULTS: dict = {
    "timezone": "",            # IANA name; empty = the machine's local zone
    "locale": "en",            # en | ru — table headers and report phrases
    "channels": [],            # ["@channel"] — channel stories (counts only: Telegram hides channel viewers)
    "poll": {
        "idle_s": 3600,             # no active rule: check this often instead (0 = always at full speed)
        "counters_s": 60,           # view counters of active stories
        "new_stories_s": 300,       # look for newly posted stories
        "pinned_s": 1800,           # profile (pinned) stories keep collecting views after 48 h
        "pinned_max_age_days": 30,
        "channels_s": 900,
        "channel_stats_s": 3600,
        "snapshot_s": 600,          # counter snapshots during the first 48 h (growth curve)
        "reactions_refresh_s": 600, # full re-read of a list when only reactions changed
        "chat_segments_s": 900,     # member lists of chat segments ("only the members of this group")
        "page_pause_s": 0.7,
    },
    "backfill": {"pause_s": 2.0, "thumbs": True},
    "notify": {
        "via": "auto",              # auto | bot | saved | none
        "chat_id": "",              # bot target; empty = TELEGRAM_HOME_CHANNEL
        "thread_id": "",            # forum topic; empty = TELEGRAM_HOME_CHANNEL_THREAD_ID
        "rich": True,               # Bot API 10.1 sendRichMessage (native tables)
    },
    "pulse": {"enabled": True, "after_hours": 2, "baseline_stories": 20},
    "digest": {"enabled": True, "weekday": 6, "time": "20:00", "monthly_day": 1},
    "tables": {"max_rows": 150, "name_links": "never", "nick_links": "link", "csv_delimiter": ","},
    "autoresponder": {
        "enabled": False,           # global switch; rules stay in shadow while false
        "default_scope": "contacts",  # contacts | dialog | all
        "caps": {"contacts": 40, "dialog": 40, "all": 10, "per_hour": 15},
        "delay_s": [120, 360],
        "min_delay_s": 30,
        "quiet_hours": ["22:00", "09:00"],
        "cooldown_days": 7,
        "skip_if_owner_wrote_hours": 24,
        "never_message": [],        # user ids or @usernames that never get an automatic message
    },
    "session_env": "STORIES_SESSION_STRING",
}


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def config_path() -> Path:
    return Path(os.environ.get("STORIES_CONFIG") or (hermes_home() / "telegram-stories.json"))


def data_dir() -> Path:
    return Path(os.environ.get("STORIES_HOME") or (hermes_home() / "data" / "telegram-stories"))


def env_file() -> Path:
    return Path(os.environ.get("STORIES_ENV_FILE") or (hermes_home() / ".env"))


def db_path() -> Path:
    return data_dir() / "stories.db"


# ── secrets ────────────────────────────────────────────────────────────────

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def read_env_file(path: Path | None = None) -> dict:
    path = path or env_file()
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        if not m or line.lstrip().startswith("#"):
            continue
        val = m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out.setdefault(m.group(1), val)
    return out


def secret(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    if val:
        return val.strip()
    return (read_env_file().get(name) or default).strip()


def write_env_value(key: str, value: str, path: Path | None = None) -> None:
    """Set KEY=value in the env file, keeping every other line and the file mode.

    Written through a temp file in the same directory and swapped in atomically,
    then restricted to the owner (0600).
    """
    path = path or env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    newline = "\n"
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        if "\r\n" in raw:
            newline = "\r\n"
        lines = raw.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        m = _ENV_LINE.match(line)
        if m and m.group(1) == key and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={value}"
            replaced = True
    if not replaced:
        lines.append(f"{key}={value}")
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(newline.join(lines) + newline)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ── config ─────────────────────────────────────────────────────────────────

def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    user: dict = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        try:
            user = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise SystemExit(f"config {path} is not valid JSON: {exc}") from exc
    user = {k: v for k, v in user.items() if not k.startswith("_")}
    return _merge(DEFAULTS, user)


def get_tz(cfg: dict) -> tzinfo:
    name = (cfg.get("timezone") or "").strip()
    if name.upper() in ("UTC", "ETC/UTC", "Z"):
        return timezone.utc
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 — no tzdata (Windows without the tzdata package) or a bad name
            import logging
            logging.getLogger("telegram-stories").warning(
                "timezone %r is not available here — using the machine's local zone", name)
    from datetime import datetime
    local = datetime.now().astimezone().tzinfo
    return local or timezone.utc


def api_credentials() -> tuple[int, str]:
    api_id = secret("STORIES_API_ID") or secret("TG_API_ID")
    api_hash = secret("STORIES_API_HASH") or secret("TG_API_HASH")
    if not api_id or not api_hash:
        return 0, ""
    try:
        return int(api_id), api_hash
    except ValueError:
        return 0, ""
