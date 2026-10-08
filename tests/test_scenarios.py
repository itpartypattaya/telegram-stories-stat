"""The automatic-message scenarios the README promises, end to end on synthetic data."""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from _helpers import TempHome, story, user

from tgstories import collector, config, db, rules, sender

T0 = 1_760_000_000          # 2025-10-09 08:53:20 UTC


def setup(home, locale="en"):
    # the scenarios replay fixed dates: the age of an event (max_event_age_h) has its own tests
    cfg = {"timezone": "UTC", "locale": locale,
           "autoresponder": {"enabled": True, "quiet_hours": [], "delay_s": [30, 30], "skip_if_owner_wrote_hours": 0,
                             "max_event_age_h": 0}}
    (home / "telegram-stories.json").write_text(json.dumps(cfg), encoding="utf-8")
    cfg = config.load_config()
    con = db.connect()
    db.set_meta(con, "owner_id", 1)
    db.upsert_person(con, collector.tg.user_row(user(10, "Ann", None, "ann", contact=True)))
    db.upsert_story(con, collector.tg.story_row(story(100, T0, caption="part 1"), 1))
    db.upsert_story(con, collector.tg.story_row(story(101, T0 + 5, caption="part 2 — #guide inside"), 1))
    return con, cfg


def create(cfg, spec) -> str:
    out = []
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rules.cli(cfg, SimpleNamespace(action="create", id=None, json=json.dumps(spec), file=None, confirm=None))
    out.append(buf.getvalue())
    return "".join(out)


class Scenarios(unittest.TestCase):
    def test_lead_magnet_only_on_the_tagged_last_story(self):
        with TempHome() as home:
            con, cfg = setup(home)
            create(cfg, {"name": "guide", "stories": {"mode": "tag", "tag": "#guide"}, "audience": {"mode": "all"},
                         "trigger": "view", "action": {"type": "dm", "text": "Here is the guide: https://x.y/g"}})
            con.execute("UPDATE rules SET status='active'")
            eng = rules.Engine(con, cfg)
            eng.on_event("view", {"peer_id": 1, "story_id": 100, "user_id": 10, "viewed_at": T0 + 60})
            self.assertEqual(con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)   # first part: nothing
            eng.on_event("view", {"peer_id": 1, "story_id": 101, "user_id": 10, "viewed_at": T0 + 90})
            self.assertEqual(con.execute("SELECT story_id, status FROM deliveries").fetchone()[:], (101, "queued"))

    def test_rule_for_someone_who_never_viewed_waits_for_them(self):
        with TempHome() as home:
            con, cfg = setup(home)
            out = create(cfg, {"name": "winners", "stories": {"mode": "ids", "ids": [101]}, "scope": "all",
                               "audience": {"mode": "users", "users": ["@winner"]},
                               "action": {"type": "dm", "text": "You saw it — noted."}})
            self.assertIn("no views yet", out)                       # the preview says so instead of failing
            eng = rules.Engine(con, cfg)
            con.execute("UPDATE rules SET status='active'")
            eng.on_event("view", {"peer_id": 1, "story_id": 101, "user_id": 10, "viewed_at": T0 + 60})
            self.assertEqual(con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)   # Ann is not @winner
            # @winner opens the story for the first time: the collector stores the profile, then the event
            db.upsert_person(con, collector.tg.user_row(user(20, "Win", None, "winner")))
            eng.on_event("view", {"peer_id": 1, "story_id": 101, "user_id": 20, "viewed_at": T0 + 120})
            self.assertEqual(con.execute("SELECT user_id, status FROM deliveries").fetchone()[:], (20, "queued"))

    def test_reply_to_get_the_guide_works_for_a_first_time_viewer(self):
        with TempHome() as home:
            con, cfg = setup(home)
            create(cfg, {"name": "reply for the guide", "stories": {"mode": "tag", "tag": "#guide"},
                         "audience": {"mode": "all"}, "scope": "dialog", "trigger": "reply",
                         "action": {"type": "dm", "text": "Here it is: https://x.y/g"}})
            con.execute("UPDATE rules SET status='active'")
            # Ann was checked an hour ago and had never written — that answer is cached for a day
            con.execute("UPDATE people SET has_dialog=0, dialog_checked_at=? WHERE user_id=10", (db.now(),))
            eng = rules.Engine(con, cfg)
            # a stranger replies seconds after opening the story, before the next viewer-list read
            for uid, who in ((30, user(30, "New", None, "newbie")), (10, None)):
                ev = collector.ingest_private_message(con, 1, who, user_id=uid, msg_id=uid, at=T0 + 100,
                                                      text_len=4, story_id=101)
                eng.on_event("reply", ev)
            self.assertEqual(dict(con.execute("SELECT user_id, status FROM deliveries").fetchall()),
                             {30: "queued", 10: "queued"})
            con.execute("UPDATE deliveries SET due_at=0")
            sent = []

            class Client:
                async def send_message(self, peer, text, **kw):
                    sent.append((peer.user_id, text))
                    return SimpleNamespace(id=len(sent))

                async def get_messages(self, peer, **kw):
                    return [SimpleNamespace()]          # they have written: the dialog exists

            class Api:
                client = Client()

                async def input_user(self, user_id, access_hash):
                    return SimpleNamespace(user_id=user_id)

                async def fresh_user(self, peer):
                    return None

            asyncio.run(sender.Sender(Api(), con, cfg).run_once())
            self.assertEqual(sorted(u for u, _ in sent), [10, 30])

    def test_warm_up_segment_filled_by_one_rule_reaches_the_next(self):
        with TempHome() as home:
            con, cfg = setup(home)
            create(cfg, {"name": "mark", "stories": {"mode": "ids", "ids": [100]}, "audience": {"mode": "all"},
                         "action": {"type": "segment", "segment": "warm"}})
            create(cfg, {"name": "follow-up", "stories": {"mode": "ids", "ids": [101]},
                         "audience": {"mode": "segment", "segment": "warm"},
                         "action": {"type": "dm", "text": "Since you watched the first part…"}})
            con.execute("UPDATE rules SET status='active'")
            eng = rules.Engine(con, cfg)
            eng.on_event("view", {"peer_id": 1, "story_id": 100, "user_id": 10, "viewed_at": T0 + 60})
            con.execute("UPDATE deliveries SET due_at=0")
            asyncio.run(sender.Sender(SimpleNamespace(client=None), con, cfg).run_once())   # no segment made by hand
            eng.on_event("view", {"peer_id": 1, "story_id": 101, "user_id": 10, "viewed_at": T0 + 600})
            self.assertEqual(con.execute("SELECT status FROM deliveries WHERE rule_id=2").fetchone()[0], "queued")

    def test_owner_notification_names_the_moment_of_the_view(self):
        with TempHome() as home:
            con, cfg = setup(home, locale="ru")
            n = rules.normalize({"name": "кто увидел", "stories": {"mode": "ids", "ids": [101]},
                                 "audience": {"mode": "users", "users": ["@ann"]}, "action": {"type": "notify"}},
                                cfg)
            con.execute("INSERT INTO rules(id,name,status,spec,digest) VALUES(1,'n','active',?,?)",
                        (db.dumps(n), rules.digest(n)))
            con.execute("INSERT INTO views(peer_id,story_id,user_id,first_viewed_at,viewed_at) VALUES(1,101,10,?,?)",
                        (T0 + 180, T0 + 900))
            con.execute("INSERT INTO deliveries(rule_id,user_id,peer_id,story_id,status,due_at,created_at) "
                        "VALUES(1,10,1,101,'queued',0,?)", (db.now(),))
            got = []

            async def notify(md):
                got.append(md)
                return True

            s = sender.Sender(SimpleNamespace(client=None), con, cfg, notify=notify)
            asyncio.run(s.run_once())
            self.assertEqual(len(got), 1)
            self.assertIn("09.10 08:56", got[0])          # first opened at T0+180, not the later re-view
            self.assertIn("Сторис 101", got[0])
            self.assertIn("правило «кто увидел»", got[0])


if __name__ == "__main__":
    unittest.main()
