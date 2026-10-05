"""Telegram side: the Telethon client, a thin API adapter and TL → row converters.

Telethon is imported lazily so that tables, reports and tests work on any
Python without it. Converters look at class names instead of isinstance(), so
tests can feed them plain objects.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from . import __version__, config

log = logging.getLogger("telegram-stories")

DEVICE_MODEL = "Hermes Stories"


def _ts(dt) -> int | None:
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if isinstance(dt, datetime):
        return int(dt.timestamp())
    return None


def _name(obj) -> str:
    return type(obj).__name__


# ── converters ─────────────────────────────────────────────────────────────

def reaction_str(reaction) -> str | None:
    if reaction is None:
        return None
    kind = _name(reaction)
    if kind == "ReactionEmoji":
        return getattr(reaction, "emoticon", None)
    if kind == "ReactionCustomEmoji":
        return f"custom:{getattr(reaction, 'document_id', '')}"
    if kind == "ReactionPaid":
        return "paid"
    return kind


def user_row(u) -> dict | None:
    if u is None or _name(u) not in ("User", "UserFull") and not hasattr(u, "first_name"):
        return None
    usernames = []
    for item in getattr(u, "usernames", None) or []:
        if getattr(item, "active", True) and getattr(item, "username", None):
            usernames.append(item.username)
    username = getattr(u, "username", None) or (usernames[0] if usernames else None)
    is_min = bool(getattr(u, "min", False))

    def flag(attr):
        # a "min" object, or an older layer without the field, says nothing — None, not False
        if is_min or not hasattr(u, attr):
            return None
        return int(bool(getattr(u, attr)))

    paid = None
    if not is_min and hasattr(u, "send_paid_messages_stars"):
        paid = int(getattr(u, "send_paid_messages_stars") or 0)
    return {
        "user_id": int(u.id),
        "access_hash": getattr(u, "access_hash", None) if not is_min else None,
        "first_name": getattr(u, "first_name", None),
        "last_name": getattr(u, "last_name", None),
        "username": username,
        "usernames": json.dumps(usernames, ensure_ascii=False) if usernames else None,
        "is_contact": flag("contact"),
        "mutual": flag("mutual_contact"),
        "close_friend": flag("close_friend"),
        "premium": flag("premium"),
        "bot": flag("bot"),
        "deleted": flag("deleted"),
        "paid_stars": paid,
    }


def _privacy(story) -> str:
    if getattr(story, "public", False):
        return "everyone"
    if getattr(story, "close_friends", False):
        return "close_friends"
    if getattr(story, "contacts", False):
        return "contacts"
    if getattr(story, "selected_contacts", False):
        return "selected"
    rules = [_name(r) for r in (getattr(story, "privacy", None) or [])]
    if "PrivacyValueAllowAll" in rules:
        return "everyone"
    if "PrivacyValueAllowCloseFriends" in rules:
        return "close_friends"
    if "PrivacyValueAllowContacts" in rules:
        return "contacts"
    return "selected" if rules else ""


def _media(story) -> tuple[str, float | None]:
    media = getattr(story, "media", None)
    if media is None:
        return "", None
    kind = _name(media)
    if kind == "MessageMediaPhoto":
        return "photo", None
    if kind == "MessageMediaDocument":
        doc = getattr(media, "document", None)
        for attr in getattr(doc, "attributes", None) or []:
            if _name(attr) == "DocumentAttributeVideo":
                return "video", float(getattr(attr, "duration", 0) or 0)
        return "document", None
    return kind.replace("MessageMedia", "").lower(), None


def _entities(story) -> str | None:
    counts: dict = {}
    for e in getattr(story, "entities", None) or []:
        n = _name(e).replace("MessageEntity", "").lower()
        if n in ("url", "texturl"):
            n = "link"
        elif n in ("mention", "mentionname", "inputmessageentitymentionname"):
            n = "mention"
        counts[n] = counts.get(n, 0) + 1
    return json.dumps(counts, sort_keys=True) if counts else None


def _areas(story) -> str | None:
    kinds = []
    for a in getattr(story, "media_areas", None) or []:
        kinds.append(_name(a).replace("MediaArea", "").lower())
    return json.dumps(sorted(kinds)) if kinds else None


def story_row(story, peer_id: int) -> dict | None:
    kind = _name(story)
    if kind == "StoryItemDeleted":
        return {"peer_id": peer_id, "story_id": int(story.id), "deleted": 1}
    if kind == "StoryItemSkipped" or getattr(story, "min", False):
        return None
    media_kind, duration = _media(story)
    views = getattr(story, "views", None)
    reactions_json = None
    if views is not None and getattr(views, "reactions", None):
        rc = {}
        for r in views.reactions:
            key = reaction_str(getattr(r, "reaction", None)) or "?"
            rc[key] = rc.get(key, 0) + int(getattr(r, "count", 0) or 0)
        reactions_json = json.dumps(rc, ensure_ascii=False, sort_keys=True)
    return {
        "peer_id": peer_id,
        "story_id": int(story.id),
        "posted_at": _ts(getattr(story, "date", None)),
        "expire_at": _ts(getattr(story, "expire_date", None)),
        "pinned": int(bool(getattr(story, "pinned", False))),
        "privacy": _privacy(story),
        "media_kind": media_kind,
        "duration": duration,
        "caption": getattr(story, "caption", None) or None,
        "entities": _entities(story),
        "areas": _areas(story),
        "views": getattr(views, "views_count", None) if views is not None else None,
        "reactions": getattr(views, "reactions_count", None) if views is not None else None,
        "forwards": getattr(views, "forwards_count", None) if views is not None else None,
        "reactions_json": reactions_json,
        "deleted": 0,
    }


def view_item(v, peer_id: int, story_id: int) -> tuple[str, dict] | None:
    """('view', row) for a viewer; ('public', row) for a public forward or repost."""
    kind = _name(v)
    if kind == "StoryView":
        return "view", {
            "peer_id": peer_id, "story_id": story_id, "user_id": int(v.user_id),
            "viewed_at": _ts(v.date),
            "reaction": reaction_str(getattr(v, "reaction", None)),
            "blocked": int(bool(getattr(v, "blocked", False))),
            "hidden_from": int(bool(getattr(v, "blocked_my_stories_from", False))),
        }
    if kind == "StoryViewPublicForward":
        msg = getattr(v, "message", None)
        actor = _peer_id(getattr(msg, "peer_id", None)) or _peer_id(getattr(msg, "from_id", None))
        return "public", {"peer_id": peer_id, "story_id": story_id, "actor_id": actor, "kind": "forward",
                          "value": str(getattr(msg, "id", "")), "at": _ts(getattr(msg, "date", None))}
    if kind == "StoryViewPublicRepost":
        actor = _peer_id(getattr(v, "peer_id", None))
        st = getattr(v, "story", None)
        return "public", {"peer_id": peer_id, "story_id": story_id, "actor_id": actor, "kind": "repost",
                          "value": str(getattr(st, "id", "")), "at": _ts(getattr(st, "date", None))}
    return None


def reaction_item(r, peer_id: int, story_id: int) -> dict | None:
    """Channel story reactions list (stories.getStoryReactionsList)."""
    kind = _name(r)
    if kind == "StoryReaction":
        return {"peer_id": peer_id, "story_id": story_id, "actor_id": _peer_id(r.peer_id), "kind": "reaction",
                "value": reaction_str(getattr(r, "reaction", None)), "at": _ts(getattr(r, "date", None))}
    if kind == "StoryReactionPublicForward":
        msg = getattr(r, "message", None)
        return {"peer_id": peer_id, "story_id": story_id, "actor_id": _peer_id(getattr(msg, "peer_id", None)),
                "kind": "forward", "value": str(getattr(msg, "id", "")), "at": _ts(getattr(msg, "date", None))}
    if kind == "StoryReactionPublicRepost":
        st = getattr(r, "story", None)
        return {"peer_id": peer_id, "story_id": story_id, "actor_id": _peer_id(getattr(r, "peer_id", None)),
                "kind": "repost", "value": str(getattr(st, "id", "")), "at": _ts(getattr(st, "date", None))}
    return None


def _peer_id(peer) -> int | None:
    if peer is None:
        return None
    for attr in ("user_id", "channel_id", "chat_id"):
        val = getattr(peer, attr, None)
        if val:
            return -int(val) if attr != "user_id" else int(val)
    return None


# ── client ─────────────────────────────────────────────────────────────────

def require_telethon():
    try:
        import telethon  # noqa: F401
    except ImportError as exc:
        raise SystemExit("telethon is not installed for this Python — run scripts/install.py --install-deps "
                         "or use the interpreter recorded by `stories.py doctor`") from exc


def new_client(session_string: str = ""):
    require_telethon()
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    api_id, api_hash = config.api_credentials()
    if not api_id:
        raise SystemExit("no Telegram API credentials: set STORIES_API_ID and STORIES_API_HASH "
                         "(from https://my.telegram.org) in the env file, or run `stories.py login`")
    client = TelegramClient(StringSession(session_string or None), api_id, api_hash,
                            device_model=DEVICE_MODEL, system_version="telegram-stories",
                            app_version=__version__, receive_updates=True)
    client.parse_mode = None
    return client


async def connect(cfg: dict):
    session = config.secret(cfg.get("session_env") or "STORIES_SESSION_STRING")
    if not session:
        raise SystemExit("no stories session yet — run `stories.py login` in a terminal first")
    client = new_client(session)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit("the stories session is no longer authorized (terminated in Settings → Devices?) — "
                         "run `stories.py login` again")
    return client


class Api:
    """The handful of MTProto calls the collector needs, with FloodWait handling."""

    def __init__(self, client, *, max_wait: int = 900):
        self.client = client
        self.max_wait = max_wait
        from telethon.tl.functions import stories, stats
        self._stories = stories
        self._stats = stats

    async def call(self, request):
        from telethon.errors import FloodWaitError
        for attempt in range(3):
            try:
                return await self.client(request)
            except FloodWaitError as exc:
                wait = int(getattr(exc, "seconds", 60) or 60)
                if wait > self.max_wait or attempt == 2:
                    raise
                log.warning("flood wait %ss on %s", wait, type(request).__name__)
                await asyncio.sleep(wait * 1.2 + 1)
        return None

    async def peer_stories(self, peer):
        return await self.call(self._stories.GetPeerStoriesRequest(peer=peer))

    async def stories_views(self, peer, ids):
        return await self.call(self._stories.GetStoriesViewsRequest(peer=peer, id=list(ids)))

    async def views_list(self, peer, story_id, offset="", limit=100):
        return await self.call(self._stories.GetStoryViewsListRequest(peer=peer, id=story_id, offset=offset,
                                                                      limit=limit))

    async def archive(self, peer, offset_id=0, limit=100):
        return await self.call(self._stories.GetStoriesArchiveRequest(peer=peer, offset_id=offset_id, limit=limit))

    async def pinned(self, peer, offset_id=0, limit=100):
        return await self.call(self._stories.GetPinnedStoriesRequest(peer=peer, offset_id=offset_id, limit=limit))

    async def by_id(self, peer, ids):
        return await self.call(self._stories.GetStoriesByIDRequest(peer=peer, id=list(ids)))

    async def reactions_list(self, peer, story_id, offset=None, limit=100):
        return await self.call(self._stories.GetStoryReactionsListRequest(peer=peer, id=story_id, limit=limit,
                                                                          offset=offset))

    async def story_stats(self, peer, story_id):
        res = await self.call(self._stats.GetStoryStatsRequest(peer=peer, id=story_id))
        out = {}
        for kind in ("views_graph", "reactions_by_emotion_graph"):
            g = getattr(res, kind, None)
            if _name(g) == "StatsGraphAsync":
                g = await self.call(self._stats.LoadAsyncGraphRequest(token=g.token))
            if _name(g) == "StatsGraph":
                out[kind] = g.json.data
        return out

    async def common_chats(self, user, limit=20):
        from telethon.tl.functions.messages import GetCommonChatsRequest
        return await self.call(GetCommonChatsRequest(user_id=user, max_id=0, limit=limit))

    async def entity(self, ref):
        return await self.client.get_entity(ref)

    async def fresh_user(self, peer) -> dict | None:
        """Full current profile (contact status, Stars price) — used right before an automatic message."""
        from telethon.tl.functions.users import GetUsersRequest
        res = await self.call(GetUsersRequest(id=[peer]))
        return user_row(res[0]) if res else None

    async def input_user(self, user_id: int, access_hash: int | None):
        from telethon.tl.types import InputPeerUser
        if access_hash:
            return InputPeerUser(user_id=user_id, access_hash=access_hash)
        return await self.client.get_input_entity(user_id)
