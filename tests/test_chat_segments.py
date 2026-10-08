"""Chat segments: "only the members of this group" — the member list and the check right before sending."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import unittest
from types import SimpleNamespace

from _helpers import TempHome, story, user

from tgstories import chats, collector, config, db, rules, sender

T0 = 1_760_000_000
IN_CHAT, OUTSIDER = 10, 20           # both have written to the owner; only the first is in the group


class FakeApi:
    """The group calls of tg.Api, answered from a set of member ids."""

    def __init__(self, members):
        self.members = set(members)
        self.fail_list = self.fail_check = False
        self.found = 0
        self.sent = []
        api = self

        class Client:
            async def send_message(self, peer, text, **kw):
                api.sent.append((peer.user_id, text))
                return SimpleNamespace(id=len(api.sent))

        self.client = Client()

    async def find_chat(self, source):
        self.found += 1
        return SimpleNamespace(id=1234567890, access_hash=77, title="Vibe coders")

    @staticmethod
    def chat_ref(ent):
        return {"type": "channel", "id": ent.id, "hash": ent.access_hash}

    @staticmethod
    def input_chat(ref):
        return ("channel", ref["id"])

    async def chat_member_ids(self, peer):
        if self.fail_list:
            raise PermissionError("members hidden")
        return set(self.members)

    async def chat_has_member(self, peer, user_peer, user_id):
        if self.fail_check:
            raise ConnectionError("network")
        return user_id in self.members

    async def input_user(self, user_id, access_hash):
        return SimpleNamespace(user_id=user_id)

    async def fresh_user(self, peer):
        return None


def setup(home):
    # fixed dates: the age of an event (max_event_age_h) has its own tests
    cfg = {"timezone": "UTC", "autoresponder": {"enabled": True, "quiet_hours": [], "delay_s": [30, 30],
                                                "skip_if_owner_wrote_hours": 0, "default_scope": "dialog",
                                                "max_event_age_h": 0}}
    (home / "telegram-stories.json").write_text(json.dumps(cfg), encoding="utf-8")
    cfg = config.load_config()
    con = db.connect()
    db.set_meta(con, "owner_id", 1)
    for uid, nick in ((IN_CHAT, "member"), (OUTSIDER, "outsider")):
        db.upsert_person(con, collector.tg.user_row(user(uid, nick.title(), None, nick)))
        con.execute("UPDATE people SET has_dialog=1, dialog_checked_at=? WHERE user_id=?", (db.now(), uid))
    db.upsert_story(con, collector.tg.story_row(story(222, T0), 1))
    con.execute("INSERT INTO segments(name,kind,source,created_at) VALUES('vibe','chat','-1001234567890',0)")
    return con, cfg


def make_rule(con, cfg) -> None:
    spec = {"name": "invite", "stories": {"mode": "all"}, "audience": {"mode": "segment", "segment": "vibe"},
            "scope": "dialog", "action": {"type": "dm", "text": "Hi!"}}
    with contextlib.redirect_stdout(io.StringIO()):
        rules.cli(cfg, SimpleNamespace(action="create", id=None, json=json.dumps(spec), file=None, confirm=None))
    con.execute("UPDATE rules SET status='active'")


def view(con, cfg, uid):
    rules.Engine(con, cfg).on_event("view", {"peer_id": 1, "story_id": 222, "user_id": uid, "viewed_at": T0 + 60})


def send(con, cfg, api):
    con.execute("UPDATE deliveries SET due_at=0 WHERE status='queued'")
    asyncio.run(sender.Sender(api, con, cfg).run_once())
    return dict(con.execute("SELECT user_id, status || ':' || COALESCE(reason,'') FROM deliveries").fetchall())


class ChatSegments(unittest.TestCase):
    def test_source_forms(self):
        for given, want in (("@vibe_chat", "@vibe_chat"), ("vibe_chat", "@vibe_chat"),
                            ("https://t.me/vibe_chat", "@vibe_chat"), ("t.me/vibe_chat/", "@vibe_chat"),
                            ("-1001234567890", "-1001234567890")):
            self.assertEqual(chats.normalize_source(given), want)
        for bad in ("", "https://t.me/+AbCdEf", "t.me/joinchat/AbCd", "a b"):
            with self.assertRaises(SystemExit):
                chats.normalize_source(bad)

    def test_member_list_is_read_and_a_failed_read_keeps_the_old_one(self):
        with TempHome() as home:
            con, cfg = setup(home)
            api = FakeApi({IN_CHAT, 30})
            asyncio.run(chats.refresh_all(api, con))
            self.assertEqual(rules.segment_members(con, "vibe"), {IN_CHAT, 30})
            self.assertEqual(chats.info(con, "vibe")["title"], "Vibe coders")
            api.fail_list = True
            asyncio.run(chats.refresh_all(api, con))
            self.assertEqual(rules.segment_members(con, "vibe"), {IN_CHAT, 30})   # not emptied by an error
            self.assertIn("PermissionError", chats.info(con, "vibe")["error"])
            self.assertEqual(api.found, 1)                                     # the chat is looked up once

    def test_someone_who_wrote_to_you_but_is_not_in_the_group_gets_nothing(self):
        with TempHome() as home:
            con, cfg = setup(home)
            api = FakeApi({IN_CHAT})
            asyncio.run(chats.refresh_all(api, con))
            make_rule(con, cfg)
            view(con, cfg, OUTSIDER)
            view(con, cfg, IN_CHAT)
            self.assertEqual(send(con, cfg, api), {IN_CHAT: "sent:"})
            self.assertEqual(api.sent, [(IN_CHAT, "Hi!")])

    def test_left_the_group_after_the_list_was_read(self):
        with TempHome() as home:
            con, cfg = setup(home)
            api = FakeApi({IN_CHAT})
            asyncio.run(chats.refresh_all(api, con))
            make_rule(con, cfg)
            view(con, cfg, IN_CHAT)                     # queued from the list
            api.members.discard(IN_CHAT)                # leaves before the message is due
            self.assertEqual(send(con, cfg, api), {IN_CHAT: "skipped:not_in_chat"})
            self.assertEqual(api.sent, [])
            self.assertNotIn(IN_CHAT, rules.segment_members(con, "vibe"))

    def test_no_clear_answer_means_no_message(self):
        with TempHome() as home:
            con, cfg = setup(home)
            api = FakeApi({IN_CHAT})
            asyncio.run(chats.refresh_all(api, con))
            make_rule(con, cfg)
            view(con, cfg, IN_CHAT)
            api.fail_check = True
            self.assertEqual(send(con, cfg, api), {IN_CHAT: "skipped:chat_unverified"})
            self.assertEqual(api.sent, [])

    def test_preview_names_the_chat_and_the_check(self):
        with TempHome() as home:
            con, cfg = setup(home)
            asyncio.run(chats.refresh_all(FakeApi({IN_CHAT}), con))
            make_rule(con, cfg)
            text = rules._summary(con, cfg, con.execute("SELECT * FROM rules").fetchone())
            self.assertIn("members of the chat", text)
            self.assertIn("'Vibe coders', 1 people", text)
            self.assertIn("checked with Telegram again right before a message", text)

    def test_a_rule_on_a_missing_segment_says_so(self):
        with TempHome() as home:
            con, cfg = setup(home)
            con.execute("DELETE FROM segments")
            make_rule(con, cfg)
            text = rules._summary(con, cfg, con.execute("SELECT * FROM rules").fetchone())
            self.assertIn("does not exist yet — matches nobody", text)

    def test_delete_forgets_the_chat(self):
        with TempHome() as home:
            con, cfg = setup(home)
            asyncio.run(chats.refresh_all(FakeApi({IN_CHAT}), con))
            with contextlib.redirect_stdout(io.StringIO()):
                rules.segment_cli(cfg, SimpleNamespace(action="delete", name="vibe", members=[]))
            self.assertEqual(chats.info(con, "vibe"), {})
            self.assertEqual(rules.segment_members(con, "vibe"), set())


if __name__ == "__main__":
    unittest.main()
