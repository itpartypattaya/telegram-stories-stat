"""Notifications to the owner: pulse, digests, alerts.

via = auto   bot token + chat known → Bot API, otherwise Saved Messages
      bot    Bot API: sendRichMessage (Bot API 10.1, native tables) with a sendMessage fallback
      saved  the stories session writes to its own Saved Messages (plain text, no push notification)
      none   nothing is sent

The bot token and chat default to the agent's own (TELEGRAM_BOT_TOKEN,
TELEGRAM_HOME_CHANNEL, TELEGRAM_HOME_CHANNEL_THREAD_ID), overridable with
STORIES_BOT_TOKEN and notify.chat_id / notify.thread_id in the config.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

from . import config

log = logging.getLogger("telegram-stories")

RICH_LIMIT = 32768
PLAIN_LIMIT = 4096


def _target(cfg: dict) -> tuple[str, str, str]:
    n = cfg.get("notify", {})
    token = config.secret("STORIES_BOT_TOKEN") or config.secret("TELEGRAM_BOT_TOKEN")
    chat = str(n.get("chat_id") or config.secret("TELEGRAM_HOME_CHANNEL") or "").strip()
    thread = str(n.get("thread_id") or ("" if n.get("chat_id") else config.secret("TELEGRAM_HOME_CHANNEL_THREAD_ID"))
                 or "").strip()
    return token, chat, thread


def mode(cfg: dict) -> str:
    via = (cfg.get("notify", {}).get("via") or "auto").lower()
    if via != "auto":
        return via
    token, chat, _ = _target(cfg)
    return "bot" if token and chat else "saved"


def to_plain(md: str) -> str:
    """Readable plain-text fallback for a Markdown message with tables."""
    out = []
    for line in md.splitlines():
        s = line.strip()
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", s):
            continue
        if s.startswith("|") and s.endswith("|"):
            # split on unescaped pipes only: a name "A|B" is stored as "A\|B" and must stay one cell
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", s[1:-1])]
            s = " · ".join(c for c in cells if c)
        s = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", lambda m: m.group(1) if m.group(1).startswith("@") else m.group(1), s)
        s = s.replace("**", "").replace("__", "")
        s = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", s)
        s = re.sub(r"\\(.)", r"\1", s)
        out.append(s)
    return "\n".join(out)


def _bot_call(token: str, method: str, payload: dict, timeout: int = 20) -> tuple[bool, str]:
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return bool(body.get("ok")), ""
    except urllib.error.HTTPError as exc:
        try:
            desc = json.loads(exc.read().decode("utf-8")).get("description", "")
        except Exception:  # noqa: BLE001
            desc = str(exc)
        return False, f"{exc.code} {desc}"
    except Exception as exc:  # noqa: BLE001 — network
        return False, type(exc).__name__


def send_bot(cfg: dict, md: str) -> bool:
    token, chat, thread = _target(cfg)
    if not token or not chat:
        log.warning("notify: no bot token or chat id")
        return False
    base: dict = {"chat_id": int(chat) if chat.lstrip("-").isdigit() else chat}
    if thread:
        base["message_thread_id"] = int(thread)
    if cfg.get("notify", {}).get("rich", True) and len(md) <= RICH_LIMIT:
        ok, err = _bot_call(token, "sendRichMessage", {**base, "rich_message": {"markdown": md}})
        if ok:
            return True
        log.warning("notify: sendRichMessage failed (%s) — falling back to plain text", err)
    plain = to_plain(md)
    ok = True
    for i in range(0, len(plain), PLAIN_LIMIT):
        part_ok, err = _bot_call(token, "sendMessage", {**base, "text": plain[i:i + PLAIN_LIMIT],
                                                         "link_preview_options": {"is_disabled": True}})
        if not part_ok:
            log.warning("notify: sendMessage failed (%s)", err)
        ok = ok and part_ok
    return ok


async def send_saved(client, md: str) -> bool:
    plain = to_plain(md)
    for i in range(0, len(plain), PLAIN_LIMIT):
        await client.send_message("me", plain[i:i + PLAIN_LIMIT], link_preview=False)
    return True


def send(cfg: dict, md: str) -> bool:
    """Synchronous send for the CLI (bot only; Saved Messages needs the live client)."""
    m = mode(cfg)
    if m == "none":
        return False
    if m == "bot":
        return send_bot(cfg, md)
    import asyncio
    from . import tg

    async def _go():
        client = await tg.connect(cfg)
        try:
            return await send_saved(client, md)
        finally:
            await client.disconnect()
    return asyncio.run(_go())


async def send_async(cfg: dict, md: str, client=None) -> bool:
    m = mode(cfg)
    if m == "none":
        return False
    if m == "bot":
        import asyncio
        return await asyncio.get_running_loop().run_in_executor(None, send_bot, cfg, md)
    if client is None:
        return False
    return await send_saved(client, md)
