"""Shared test helpers: locate the skill in either layout, build synthetic data, fake Telegram objects."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


def skill_dir() -> Path:
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "skills" / "telegram-stories", here.parent):
        if (cand / "scripts" / "tgstories").is_dir():
            return cand
    raise RuntimeError("telegram-stories skill not found next to the tests")


SKILL = skill_dir()
sys.path.insert(0, str(SKILL / "scripts"))


class TempHome:
    """Isolated HERMES_HOME for a test."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.get(k) for k in ("HERMES_HOME", "STORIES_HOME", "STORIES_CONFIG",
                                                   "STORIES_ENV_FILE", "STORIES_SESSION_STRING")}
        os.environ["HERMES_HOME"] = self.tmp.name
        for k in ("STORIES_HOME", "STORIES_CONFIG", "STORIES_ENV_FILE", "STORIES_SESSION_STRING"):
            os.environ.pop(k, None)
        return Path(self.tmp.name)

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            self.tmp.cleanup()
        except (OSError, PermissionError):
            pass  # Windows keeps SQLite files locked a little longer


# ── fake Telegram objects (class names matter, not types) ────────────────

def make(cls_name: str, **fields):
    cls = type(cls_name, (SimpleNamespace,), {})
    return cls(**fields)


def user(uid, first="User", last=None, username=None, contact=False, mutual=False, close=False, bot=False,
         paid=0, access_hash=111):
    return make("User", id=uid, first_name=first, last_name=last, username=username, usernames=None,
                contact=contact, mutual_contact=mutual, close_friend=close, premium=False, bot=bot, deleted=False,
                send_paid_messages_stars=paid, access_hash=access_hash, min=False)


def story(sid, date, expire=None, caption=None, views=0, reactions=0, media="photo", pinned=True):
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(date, timezone.utc)
    ex = datetime.fromtimestamp(expire or date + 48 * 3600, timezone.utc)
    m = make("MessageMediaPhoto", photo=None) if media == "photo" else make(
        "MessageMediaDocument", document=SimpleNamespace(attributes=[make("DocumentAttributeVideo", duration=12.5)],
                                                         thumbs=[]))
    return make("StoryItem", id=sid, date=dt, expire_date=ex, caption=caption, entities=None, media=m,
                media_areas=None, privacy=[make("PrivacyValueAllowAll")], pinned=pinned, public=True,
                close_friends=False, contacts=False, selected_contacts=False, min=False,
                views=make("StoryViews", views_count=views, reactions_count=reactions, forwards_count=0,
                           reactions=[]))


def view(uid, date, reaction=None, blocked=False, hidden=False):
    from datetime import datetime, timezone
    r = make("ReactionEmoji", emoticon=reaction) if reaction else None
    return make("StoryView", user_id=uid, date=datetime.fromtimestamp(date, timezone.utc), reaction=r,
                blocked=blocked, blocked_my_stories_from=hidden)


class FakeApi:
    """Answers like Telegram from an in-memory model: stories[sid] = (StoryItem, [StoryView newest first], users)."""

    def __init__(self, stories: dict, users: list):
        self.stories = stories
        self.users = users
        self.calls = []
        self.client = None

    async def peer_stories(self, peer):
        self.calls.append(("peer_stories",))
        now_items = [s for s, _ in self.stories.values()]
        return SimpleNamespace(stories=SimpleNamespace(stories=now_items), users=self.users)

    async def stories_views(self, peer, ids):
        self.calls.append(("stories_views", tuple(ids)))
        out = []
        for sid in ids:
            s, views = self.stories[sid]
            out.append(make("StoryViews", views_count=len(views),
                            reactions_count=sum(1 for v in views if v.reaction), forwards_count=0, reactions=[]))
        return SimpleNamespace(views=out, users=self.users)

    async def views_list(self, peer, story_id, offset="", limit=100):
        self.calls.append(("views_list", story_id, offset))
        _, views = self.stories[story_id]
        start = int(offset or 0)
        page = views[start:start + limit]
        nxt = str(start + limit) if start + limit < len(views) else ""
        return SimpleNamespace(count=len(views), views_count=len(views),
                               reactions_count=sum(1 for v in views if v.reaction), views=page, users=self.users,
                               next_offset=nxt)

    async def archive(self, peer, offset_id=0, limit=100):
        items = sorted((s for s, _ in self.stories.values()), key=lambda s: -s.id)
        if offset_id:
            items = [s for s in items if s.id < offset_id]
        return SimpleNamespace(stories=items[:limit], users=self.users)

    async def pinned(self, peer, offset_id=0, limit=100):
        return SimpleNamespace(stories=[], users=[])

    async def by_id(self, peer, ids):
        # like Telegram: an id it does not have is skipped without a word
        self.calls.append(("by_id", tuple(ids)))
        return SimpleNamespace(stories=[self.stories[i][0] for i in ids if i in self.stories], users=self.users)


async def no_sleep(_):
    return None
