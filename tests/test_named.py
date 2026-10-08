"""1.6.0: a rule that names a person is the owner's own choice — only_when_named lets them in, the cooldown and
"the owner wrote to them today" do not hold it back; include_seen writes to those who already viewed."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from _helpers import TempHome, story, user

from tgstories import collector, config, db, rules, sender

WIFE, FRIEND, BANNED = 10, 11, 12


class SendApi:
    """Sends into a list; the owner wrote to everyone a minute ago."""

    def __init__(self):
        self.sent = []
        api = self

        class Client:
            async def send_message(self, peer, text, **kw):
                api.sent.append((peer.user_id, text))
                return SimpleNamespace(id=len(api.sent))

            async def get_messages(self, peer, limit=1, from_user=None):
                return [SimpleNamespace(date=datetime.now(timezone.utc))]

        self.client = Client()

    async def input_user(self, user_id, access_hash):
        return SimpleNamespace(user_id=user_id)

    async def fresh_user(self, peer):
        return None


def setup(home):
    cfg = {"timezone": "UTC", "autoresponder": {"enabled": True, "quiet_hours": [], "delay_s": [30, 30],
                                                "only_when_named": ["@wife"], "never_message": ["@banned"]}}
    (home / "telegram-stories.json").write_text(json.dumps(cfg), encoding="utf-8")
    cfg = config.load_config()
    con = db.connect()
    db.set_meta(con, "owner_id", 1)
    now = db.now()
    for uid, nick in ((WIFE, "wife"), (FRIEND, "friend"), (BANNED, "banned")):
        db.upsert_person(con, collector.tg.user_row(user(uid, nick.title(), None, nick, contact=True)))
    db.upsert_story(con, collector.tg.story_row(story(224, now - 3600), 1))
    for uid in (WIFE, FRIEND, BANNED):
        db.upsert_view(con, {"peer_id": 1, "story_id": 224, "user_id": uid, "viewed_at": now - 600})
    return con, cfg


def spec(audience, **over):
    return rules.normalize({"name": "t", "stories": {"mode": "ids", "ids": [224]}, "audience": audience,
                            "scope": "contacts", "action": {"type": "dm", "text": "Как тебе фотка?)"}, **over}, {})


def run(cfg, action, rid=None, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rules.cli(cfg, SimpleNamespace(action=action, id=rid, json=kw.get("json"), file=None,
                                       confirm=kw.get("confirm")))
    return out.getvalue()


def create_and_activate(con, cfg, sp) -> tuple[int, str]:
    run(cfg, "create", json=json.dumps(sp))
    rid, digest = con.execute("SELECT id, digest FROM rules ORDER BY id DESC LIMIT 1").fetchone()
    preview = run(cfg, "preview", rid)
    return rid, preview + run(cfg, "activate", rid, confirm=digest)


class NamedPeople(unittest.TestCase):
    def test_only_a_rule_that_names_them_writes_to_them(self):
        with TempHome() as home:
            con, cfg = setup(home)
            self.assertIsNone(rules.precheck(con, cfg, spec({"mode": "users", "users": ["@wife"]}), WIFE))
            mass = spec({"mode": "all"})
            self.assertEqual(rules.precheck(con, cfg, mass, WIFE), "only_when_named")
            self.assertIsNone(rules.precheck(con, cfg, mass, FRIEND))

    def test_never_message_holds_even_by_name(self):
        with TempHome() as home:
            con, cfg = setup(home)
            self.assertEqual(rules.precheck(con, cfg, spec({"mode": "users", "users": ["@banned"]}), BANNED),
                             "never_message")

    def test_the_cooldown_does_not_hold_a_named_message(self):
        with TempHome() as home:
            con, cfg = setup(home)
            for uid in (WIFE, FRIEND):
                con.execute("INSERT INTO deliveries(rule_id,user_id,status,sent_at) VALUES(99,?,'sent',?)",
                            (uid, db.now() - 3600))
            self.assertIsNone(rules.precheck(con, cfg, spec({"mode": "users", "users": ["@wife"]}), WIFE))
            self.assertEqual(rules.precheck(con, cfg, spec({"mode": "all"}), FRIEND), "cooldown")


class IncludeSeen(unittest.TestCase):
    def test_it_needs_stories_named_by_id(self):
        with self.assertRaises(SystemExit):
            rules.normalize({"stories": {"mode": "all"}, "include_seen": True,
                             "action": {"type": "dm", "text": "x"}}, {})
        self.assertNotIn("include_seen", spec({"mode": "all"}))      # older rules keep their digest

    def test_who_already_viewed_gets_it_and_todays_chat_does_not_stop_a_named_one(self):
        with TempHome() as home:
            con, cfg = setup(home)
            named_id, out = create_and_activate(con, cfg, spec({"mode": "users", "users": ["@wife"]},
                                                               include_seen=True))
            self.assertIn("on activation: 1 message(s)", out)
            self.assertIn("1 message(s) queued", out)
            mass_id, out = create_and_activate(con, cfg, spec({"mode": "all"}, include_seen=True))
            self.assertIn("only_when_named 1", out)
            self.assertIn("never_message 1", out)
            con.execute("UPDATE deliveries SET due_at=0 WHERE status='queued'")
            api = SendApi()
            asyncio.run(sender.Sender(api, con, cfg).run_once())
            got = {(r, u): s for r, u, s in con.execute(
                "SELECT rule_id, user_id, status || ':' || COALESCE(reason,'') FROM deliveries")}
            self.assertEqual(got[(named_id, WIFE)], "sent:")
            self.assertEqual(got[(mass_id, FRIEND)], "skipped:owner_wrote_recently")
            self.assertEqual(api.sent, [(WIFE, "Как тебе фотка?)")])

    def test_without_it_the_preview_says_who_is_left_out(self):
        with TempHome() as home:
            con, cfg = setup(home)
            _, out = create_and_activate(con, cfg, spec({"mode": "users", "users": ["@wife"]}))
            self.assertIn('add "include_seen": true', out)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM deliveries WHERE status='queued'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
