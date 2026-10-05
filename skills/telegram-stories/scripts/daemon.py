#!/usr/bin/env python3
"""telegram-stories service: one process, one Telegram connection, no LLM.

Loops (intervals from the config `poll` section):
  counters     every minute: counters of active stories → new viewers → rules engine
  new stories  every 5 minutes
  profile      every 30 minutes: pinned stories younger than N days
  channels     every 15 minutes (counts, reactions, reposts)
  sender       every 15 seconds: due automatic messages (all guards re-checked)
  reports      every minute: pulse after N hours, weekly/monthly digest
Plus a handler for incoming private messages: replies to stories and replies to
automatic messages.

Exit codes: 0 stop, 3 session terminated (systemd does not restart on 3).
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tgstories import config, db, notify, reports, tg  # noqa: E402
from tgstories.collector import Collector  # noqa: E402
from tgstories.rules import Engine  # noqa: E402
from tgstories.sender import Sender  # noqa: E402

log = logging.getLogger("telegram-stories")
EXIT_SESSION_GONE = 3


def _lock():
    """One collector per data directory."""
    path = config.data_dir() / "daemon.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass
    except OSError:
        raise SystemExit("another telegram-stories service is already running for this data directory")
    return fh


async def every(seconds: float, fn, name: str, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 — keep the service alive, log the cause
            if type(exc).__name__ in ("AuthKeyUnregisteredError", "SessionRevokedError", "AuthKeyDuplicatedError",
                                      "UserDeactivatedError", "UserDeactivatedBanError"):
                raise
            log.warning("%s: %s: %s", name, type(exc).__name__, exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    for noisy in ("telethon", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _lockfile = _lock()  # noqa: F841 — held for the process lifetime
    cfg = config.load_config()
    con = db.connect()
    try:
        client = await tg.connect(cfg)
    except SystemExit as exc:
        log.error("%s", exc)
        return EXIT_SESSION_GONE
    me = await client.get_me()
    db.set_meta(con, "owner_id", me.id)
    db.set_meta(con, "owner_premium", int(bool(getattr(me, "premium", False))))
    db.set_meta(con, "python", sys.executable)
    api = tg.Api(client)
    engine = Engine(con, cfg)

    async def alert(md: str):
        await notify.send_async(cfg, md, client)

    sender = Sender(api, con, cfg, notify=alert)

    def on_event(kind, row):
        try:
            engine.on_event(kind, row)
            if kind in ("view", "view_changed") and (row.get("hidden_from") or row.get("blocked")):
                sender.on_hidden(row["user_id"])
        except Exception:  # noqa: BLE001
            log.exception("rules engine")

    col = Collector(api, con, cfg, me.id, on_view=on_event)
    poll = cfg.get("poll", {})
    stop = asyncio.Event()

    from telethon import events

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def _incoming(event):  # noqa: ANN001
        msg = event.message
        uid = event.sender_id
        hdr = getattr(msg, "reply_to", None)
        if type(hdr).__name__ == "MessageReplyStoryHeader":
            story_id = int(getattr(hdr, "story_id", 0) or 0)
            con.execute("INSERT OR IGNORE INTO story_replies(peer_id,story_id,user_id,msg_id,at,length) "
                        "VALUES(?,?,?,?,?,?)", (me.id, story_id, uid, msg.id, int(msg.date.timestamp()),
                                                len(msg.message or "")))
            on_event("reply", {"peer_id": me.id, "story_id": story_id, "user_id": uid,
                               "viewed_at": int(msg.date.timestamp())})
        sender.on_reply(uid)

    async def counters():
        await col.poll_counters(col.active_ids())
        db.set_state(con, "last_poll_at", db.now())

    async def new_stories():
        await col.refresh_active()

    async def pinned():
        ids = col.pinned_recent_ids()
        if ids:
            await col.poll_counters(ids)

    async def channels():
        for ref in cfg.get("channels") or []:
            await col.poll_channel(ref, with_stats=False)

    async def channel_stats():
        for ref in cfg.get("channels") or []:
            ent, peer_id = await col.channel_peer(ref)
            for sid in col.active_ids(peer_id):
                await col.channel_stats(ent, peer_id, sid)

    async def send():
        await sender.run_once()

    async def report():
        for sid in reports.due_pulses(con, cfg, me.id):
            text = reports.pulse_text(con, cfg, story_ref=str(sid), peer_id=me.id)
            con.execute("UPDATE stories SET pulse_sent_at=? WHERE peer_id=? AND story_id=?", (db.now(), me.id, sid))
            await alert(text)
        for key, period in reports.due_digests(con, cfg):
            reports.mark_sent(con, key)
            await alert(reports.digest_text(con, cfg, period=period, peer_id=me.id))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    await col.refresh_active()
    log.info("telegram-stories service started for %s (premium: %s); %d active stories",
             me.username or me.id, bool(getattr(me, "premium", False)), len(col.active_ids()))
    tasks = [
        asyncio.create_task(every(poll.get("counters_s", 60), counters, "counters", stop)),
        asyncio.create_task(every(poll.get("new_stories_s", 300), new_stories, "new_stories", stop)),
        asyncio.create_task(every(poll.get("pinned_s", 1800), pinned, "pinned", stop)),
        asyncio.create_task(every(15, send, "sender", stop)),
        asyncio.create_task(every(60, report, "reports", stop)),
    ]
    if cfg.get("channels"):
        tasks.append(asyncio.create_task(every(poll.get("channels_s", 900), channels, "channels", stop)))
        tasks.append(asyncio.create_task(every(poll.get("channel_stats_s", 3600), channel_stats, "channel_stats",
                                               stop)))
    disconnected = asyncio.ensure_future(client.disconnected)  # Telethon reconnects by itself; this fires for good
    stopper = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([*tasks, disconnected, stopper], return_when=asyncio.FIRST_COMPLETED)
    code = 0
    for t in done:
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            name = type(exc).__name__
            if name in ("AuthKeyUnregisteredError", "SessionRevokedError", "AuthKeyDuplicatedError"):
                log.error("the stories session was terminated (%s) — log in again with `stories.py login`", name)
                try:
                    notify.send_bot(cfg, "⛔ telegram-stories: the session was terminated in Telegram. "
                                         "Run `stories.py login` to resume collecting.")
                except Exception:  # noqa: BLE001
                    pass
                code = EXIT_SESSION_GONE
            else:
                log.error("stopped: %s: %s", name, exc)
                code = 1
    if disconnected in done and code == 0 and not stop.is_set():
        log.error("disconnected from Telegram")
        code = 1
    stop.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await client.disconnect()
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
