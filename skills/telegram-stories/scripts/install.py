#!/usr/bin/env python3
"""Install, check or remove telegram-stories.

  install.py                    data folder + database + config + service (if a session exists)
  install.py --install-deps     also pip-install Telethon into the service interpreter
  install.py --python PATH      interpreter for the service (default: this one)
  install.py --backfill         import story history right after installing
  install.py --no-service       only files and database (no systemd unit)
  install.py --check            health check (same as `stories.py doctor`)
  install.py --uninstall [--purge]   stop and remove the service; --purge also deletes the collected data

Everything is idempotent: re-running never overwrites an existing config or database.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from tgstories import __version__, config, db  # noqa: E402

UNIT_NAME = "telegram-stories.service"


def say(ok, text):
    print({True: "ok  ", False: "FAIL", None: "--  "}[ok] + " " + text)


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def telethon_version(python: str) -> str | None:
    try:
        out = subprocess.run([python, "-c", "import telethon; print(telethon.__version__)"],
                             capture_output=True, text=True, timeout=60)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def detect_timezone() -> str:
    tz = os.environ.get("TZ", "")
    if "/" in tz:
        return tz.lstrip(":")
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if name:
            return name
    except OSError:
        pass
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return ""


def ensure_config() -> None:
    path = config.config_path()
    if path.exists():
        try:
            json.loads(path.read_text(encoding="utf-8") or "{}")
            say(True, f"config kept: {path}")
        except json.JSONDecodeError as exc:
            say(False, f"config {path} is not valid JSON ({exc}) — fix it, nothing was overwritten")
        return
    example = json.loads((SKILL / "examples" / "config.example.json").read_text(encoding="utf-8"))
    example["timezone"] = detect_timezone()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(example, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    say(True, f"config created: {path} (timezone: {example['timezone'] or 'machine local'})")


def install_unit(python: str, start: bool) -> None:
    if not shutil.which("systemctl"):
        say(None, "no systemd here — run the service yourself: "
                  f"{python} {SCRIPTS / 'daemon.py'} (e.g. in tmux or with nohup)")
        return
    text = (SKILL / "templates" / UNIT_NAME).read_text(encoding="utf-8")
    text = (text.replace("@PYTHON@", python).replace("@SCRIPTS@", str(SCRIPTS))
            .replace("@HERMES_HOME@", str(config.hermes_home())))
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    changed = not path.exists() or path.read_text(encoding="utf-8") != text
    path.write_text(text, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    say(True, f"service unit {'written' if changed else 'unchanged'}: {path}")
    linger = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"],
                            capture_output=True, text=True).stdout.strip()
    if linger != "yes":
        say(None, "user lingering is off — the service stops when you log out. "
                  "An administrator enables it once with: loginctl enable-linger <your user> (as root)")
    if start:
        subprocess.run(["systemctl", "--user", "enable", UNIT_NAME], check=False, capture_output=True)
        subprocess.run(["systemctl", "--user", "restart", UNIT_NAME], check=False)
        state = subprocess.run(["systemctl", "--user", "is-active", UNIT_NAME], capture_output=True,
                               text=True).stdout.strip()
        say(state == "active", f"service {state}")
    else:
        say(None, "service not started yet — it starts after `stories.py login` and `install.py` again")


def uninstall(purge: bool) -> int:
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False, capture_output=True)
        if unit_path().exists():
            unit_path().unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        say(True, "service stopped and removed")
    if purge:
        d = config.data_dir()
        removed = 0
        for sub in ("thumbs", "exports"):
            for f in (d / sub).glob("*") if (d / sub).exists() else []:
                f.unlink()
                removed += 1
            if (d / sub).exists():
                (d / sub).rmdir()
        for name in ("stories.db", "stories.db-wal", "stories.db-shm", "daemon.lock", "login-pending.json"):
            if (d / name).exists():
                (d / name).unlink()
                removed += 1
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
        say(True, f"data removed from {d} ({removed} files)")
    session_env = config.load_config().get("session_env", "STORIES_SESSION_STRING")
    print(f"Also terminate the \"Hermes Stories\" session in Telegram (Settings → Devices) and delete "
          f"{session_env} from {config.env_file()}.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=f"telegram-stories {__version__} installer")
    p.add_argument("--check", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--purge", action="store_true")
    p.add_argument("--install-deps", action="store_true")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--no-service", action="store_true")
    p.add_argument("--backfill", action="store_true")
    args = p.parse_args()

    if args.uninstall:
        return uninstall(args.purge)
    if args.check:
        from tgstories import doctor
        return doctor.run(config.load_config(), online=True)

    if sys.version_info < (3, 10):
        say(False, "Python 3.10+ is required")
        return 1
    python = str(Path(args.python))
    ver = telethon_version(python)
    if not ver and args.install_deps:
        subprocess.run([python, "-m", "pip", "install", "telethon>=1.36,<2", "qrcode>=7"], check=False)
        ver = telethon_version(python)
    say(bool(ver) or None, f"telethon {ver} for {python}" if ver else
        f"telethon missing for {python} — rerun with --install-deps (or install it yourself)")

    d = config.data_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    for sub in ("thumbs", "exports"):
        (d / sub).mkdir(exist_ok=True)
    con = db.connect()
    db.set_meta(con, "python", python)
    db.set_meta(con, "installed_version", __version__)
    say(True, f"database ready: {config.db_path()} (schema v{con.execute('PRAGMA user_version').fetchone()[0]})")
    ensure_config()

    session = config.secret(config.load_config().get("session_env") or "STORIES_SESSION_STRING")
    if not session:
        say(None, "no Telegram session yet. Next, in a terminal:\n"
                  f"       {python} {SCRIPTS / 'stories.py'} login\n"
                  "     then run install.py again to start the service.")
    if not args.no_service:
        install_unit(python, start=bool(session and ver))

    from tgstories import doctor
    rich = doctor.rich_messages_enabled()
    if rich is False:
        say(None, "Hermes sends tables as bullet lists until you enable native tables: "
                  "platforms.telegram.extra.rich_messages: true in config.yaml")

    if args.backfill:
        if not session:
            say(False, "backfill needs the session — log in first")
        else:
            subprocess.run([python, str(SCRIPTS / "stories.py"), "backfill"], check=False)
    print("Done. Health check any time: install.py --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
