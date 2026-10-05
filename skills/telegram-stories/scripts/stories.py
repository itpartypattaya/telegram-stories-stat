#!/usr/bin/env python3
"""telegram-stories CLI.

  stories.py login [--with-code|--phone +…]   separate Telegram session by QR (run in a terminal)
  stories.py login start --phone +… | finish --code …   two-step mode for agents (no cloud password)
  stories.py doctor                           health check
  stories.py sync                             one collection pass (the service does this every minute)
  stories.py backfill [--channel @x] [--limit N]   import story history (resumable)
  stories.py table story <id|last|-N> | summary --period 30d | people | hours | compare <id> <id>…
  stories.py report pulse [<id>] | digest --period 7d
  stories.py rule template|list|show|create|update|preview|activate|shadow|pause|delete
  stories.py segment list|show|create|add|remove|delete|refresh   (--kind chat --source @group: group members)
  stories.py stop | start | status             autoresponder kill switch
  stories.py export views|stories|people [--period 90d]
  stories.py dashboard [--out FILE] [--period 30d|90d|1y|all] [--no-thumbs]   one-file HTML page
  stories.py notify-test

Table and report commands read SQLite and work on any Python >= 3.10. Tables
and the dashboard first re-read Telegram when the data is older than five
minutes (with no active rule the service checks only hourly); `--no-sync`
skips that. Commands that talk to Telegram need Telethon; if it is missing
here, the command re-runs itself with the interpreter the installer recorded.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tgstories import __version__, config, db  # noqa: E402

TELEGRAM_COMMANDS = {"login", "sync", "backfill", "notify-test"}
FRESH_COMMANDS = {"table", "dashboard"}      # re-read Telegram first when the data is old


def _reexec_with_recorded_python() -> None:
    try:
        import telethon  # noqa: F401
        return
    except ImportError:
        pass
    try:
        con = db.connect(create=False)
        py = db.get_meta(con, "python")
    except SystemExit:
        py = None
    if py and Path(py).exists() and Path(py).resolve() != Path(sys.executable).resolve():
        os.execv(py, [py, str(Path(__file__).resolve()), *sys.argv[1:]])
    from tgstories import tg
    tg.require_telethon()


def _has_telethon_or_reexec() -> bool:
    """True when Telethon is importable here; otherwise re-run with the recorded interpreter (does not return),
    or False when there is none — then the command works on the stored data."""
    try:
        import telethon  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        py = db.get_meta(db.connect(create=False), "python")
    except SystemExit:
        py = None
    if py and Path(py).exists() and Path(py).resolve() != Path(sys.executable).resolve():
        os.execv(py, [py, str(Path(__file__).resolve()), *sys.argv[1:]])
    return False


def freshen(cfg, run=None) -> bool:
    """Re-read Telegram when the last poll is older than five minutes. A failure only prints a note to stderr:
    the stored data is shown anyway. Returns True when a pass ran."""
    from tgstories import pace
    try:
        con = db.connect(create=False)
    except SystemExit:
        return False
    if not pace.stale(con):
        return False
    if run is None:
        from tgstories import collector
        run = collector.run_once
    try:
        asyncio.run(run(cfg))
        return True
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — a table without the newest views beats no table
        from datetime import datetime
        last = int(db.get_state(con, "last_poll_at", "0") or 0)
        when = datetime.fromtimestamp(last).strftime("%d.%m %H:%M") if last else "never"
        print(f"note: could not refresh from Telegram ({type(exc).__name__}); data as of {when}", file=sys.stderr)
        return False


# ── handlers ───────────────────────────────────────────────────────────────

def cmd_login(args, cfg) -> int:
    from tgstories import login
    if args.step == "start":
        if not args.phone:
            raise SystemExit("login start needs --phone")
        return asyncio.run(login.start(cfg, args.phone))
    if args.step == "finish":
        if not args.code:
            raise SystemExit("login finish needs --code")
        return asyncio.run(login.finish(cfg, args.code))
    if args.with_code or args.phone:
        return asyncio.run(login.interactive(cfg, args.phone))
    return asyncio.run(login.default(cfg))


def cmd_doctor(args, cfg) -> int:
    from tgstories import doctor
    return doctor.run(cfg, online=not args.offline)


def cmd_sync(args, cfg) -> int:
    from tgstories import collector
    return asyncio.run(collector.run_once(cfg, verbose=True))


def cmd_backfill(args, cfg) -> int:
    from tgstories import collector
    return asyncio.run(collector.run_backfill(cfg, channel=args.channel, limit=args.limit,
                                              thumbs=not args.no_thumbs, refetch=args.refetch))


def cmd_table(args, cfg) -> int:
    from tgstories import tables
    print(tables.render_cli(cfg, args))
    return 0


def cmd_report(args, cfg) -> int:
    from tgstories import reports
    con = db.connect(create=False)
    if args.kind == "pulse":
        text = reports.pulse_text(con, cfg, story_ref=args.ref)
    else:
        text = reports.digest_text(con, cfg, period=args.period)
    if args.send:
        from tgstories import notify
        ok = notify.send(cfg, text)
        print("sent" if ok else "not sent (see notify settings)")
    else:
        print(text)
    return 0


def cmd_rule(args, cfg) -> int:
    from tgstories import rules
    return rules.cli(cfg, args)


def cmd_segment(args, cfg) -> int:
    from tgstories import rules
    return rules.segment_cli(cfg, args)


def cmd_switch(args, cfg) -> int:
    con = db.connect(create=False)
    if args.cmd == "stop":
        db.set_state(con, "kill_switch", 1)
        n = con.execute("UPDATE deliveries SET status='skipped', reason='stopped' WHERE status='queued'").rowcount
        print(f"Autoresponder stopped. {n} queued message(s) cancelled. `stories.py start` resumes.")
    elif args.cmd == "start":
        db.set_state(con, "kill_switch", 0)
        db.set_state(con, "breaker_until", 0)
        print("Autoresponder kill switch released. Rules still need `active` status and "
              "autoresponder.enabled=true in the config.")
    else:
        from tgstories import doctor
        print(doctor.status_text(con, cfg))
    return 0


def cmd_export(args, cfg) -> int:
    from tgstories import tables
    path = tables.export_csv(cfg, args.what, args.period)
    print(path)
    return 0


def cmd_dashboard(args, cfg) -> int:
    from tgstories import dashboard
    print(dashboard.build(cfg, args.out, period=args.period, thumbs=not args.no_thumbs))
    return 0


def cmd_notify_test(args, cfg) -> int:
    from tgstories import notify
    text = args.text or "telegram-stories: notification test ✅"
    ok = notify.send(cfg, text)
    print("sent" if ok else "not sent — check notify.via / bot token / chat id")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stories.py", description=f"telegram-stories {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("login", help="log the service in with its own Telegram session")
    lg.add_argument("step", nargs="?", choices=["start", "finish"], help="two-step mode for agents")
    lg.add_argument("--phone", help="log in with phone + code instead of QR")
    lg.add_argument("--with-code", action="store_true", help="phone + code instead of QR")
    lg.add_argument("--code", help="the code, for `login finish`")
    lg.add_argument("--qr", action="store_true", help="QR (the default; kept for old instructions)")
    lg.set_defaults(func=cmd_login)

    dr = sub.add_parser("doctor", help="health check")
    dr.add_argument("--offline", action="store_true", help="skip the Telegram connection test")
    dr.set_defaults(func=cmd_doctor)

    sub.add_parser("sync", help="one collection pass").set_defaults(func=cmd_sync)

    bf = sub.add_parser("backfill", help="import story history")
    bf.add_argument("--channel", help="@channel from the config instead of your own stories")
    bf.add_argument("--limit", type=int, help="at most N stories this run")
    bf.add_argument("--no-thumbs", action="store_true")
    bf.add_argument("--refetch", action="store_true", help="re-read stories that were already imported")
    bf.set_defaults(func=cmd_backfill)

    tb = sub.add_parser("table", help="statistics tables")
    tb.add_argument("kind", choices=["story", "summary", "people", "hours", "compare", "channel"])
    tb.add_argument("refs", nargs="*", help="story id, `last` or -N (N-th from the end)")
    tb.add_argument("--period", help="e.g. 7d, 30d, 12w, 6m, all")
    tb.add_argument("--from", dest="date_from")
    tb.add_argument("--to", dest="date_to")
    tb.add_argument("--status", help="people: core|regular|occasional|new|cooling|lost")
    tb.add_argument("--peer", help="me (default) or @channel")
    tb.add_argument("--format", default="md", choices=["md", "csv", "json", "text"])
    tb.add_argument("--limit", type=int)
    tb.add_argument("--sort", help="people/story: column to sort by")
    tb.add_argument("--no-sync", action="store_true", help="do not re-read Telegram first, even if the data is old")
    tb.set_defaults(func=cmd_table)

    rp = sub.add_parser("report", help="pulse / digest text")
    rp.add_argument("kind", choices=["pulse", "digest"])
    rp.add_argument("ref", nargs="?")
    rp.add_argument("--period", default="7d")
    rp.add_argument("--send", action="store_true", help="send to the notification target")
    rp.set_defaults(func=cmd_report)

    ru = sub.add_parser("rule", help="autoresponder rules")
    ru.add_argument("action", choices=["template", "list", "show", "create", "update", "preview",
                                       "activate", "shadow", "pause", "delete"])
    ru.add_argument("id", nargs="?", type=int)
    ru.add_argument("--json", help="rule spec as JSON")
    ru.add_argument("--file", help="rule spec JSON file")
    ru.add_argument("--confirm", help="digest printed by `rule preview`")
    ru.set_defaults(func=cmd_rule)

    sg = sub.add_parser("segment", help="audience segments")
    sg.add_argument("action", choices=["list", "show", "create", "add", "remove", "delete", "refresh"])
    sg.add_argument("name", nargs="?")
    sg.add_argument("members", nargs="*", help="@username or numeric id")
    sg.add_argument("--kind", default="static",
                    choices=["static", "contacts", "mutual", "close_friends", "status", "chat"])
    sg.add_argument("--source", help="status name for kind=status; @group, t.me link or -100… id for kind=chat")
    sg.set_defaults(func=cmd_segment)

    for name in ("stop", "start", "status"):
        sub.add_parser(name, help="autoresponder kill switch / status").set_defaults(func=cmd_switch)

    ex = sub.add_parser("export", help="CSV export")
    ex.add_argument("what", choices=["views", "stories", "people"])
    ex.add_argument("--period", default="all")
    ex.set_defaults(func=cmd_export)

    ds = sub.add_parser("dashboard", help="one-file HTML dashboard (charts, tables, viewer lists)")
    ds.add_argument("--out", help="file to write (default: <data dir>/dashboard/dashboard.html)")
    ds.add_argument("--period", choices=["30d", "90d", "1y", "all"], help="tab open by default")
    ds.add_argument("--no-thumbs", action="store_true", help="leave story thumbnails out (smaller file)")
    ds.add_argument("--no-sync", action="store_true", help="do not re-read Telegram first, even if the data is old")
    ds.set_defaults(func=cmd_dashboard)

    nt = sub.add_parser("notify-test", help="send a test notification")
    nt.add_argument("--text")
    nt.set_defaults(func=cmd_notify_test)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # emoji in tables must not crash a legacy console
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if args.cmd in TELEGRAM_COMMANDS or (args.cmd == "doctor" and not args.offline) \
            or (args.cmd == "rule" and args.action in ("activate",)) \
            or (args.cmd == "segment" and (args.action == "refresh" or (args.action == "create" and
                                                                     args.kind == "chat"))):
        _reexec_with_recorded_python()
    fresh = False
    if args.cmd in FRESH_COMMANDS and not args.no_sync:
        try:
            from tgstories import pace
            fresh = pace.stale(db.connect(create=False)) and _has_telethon_or_reexec()
        except SystemExit:
            fresh = False
    cfg = config.load_config()
    if fresh:
        freshen(cfg)
    return int(args.func(args, cfg) or 0)


if __name__ == "__main__":
    sys.exit(main())
