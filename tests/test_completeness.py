"""Findings of the 06.10 review: what a preview counts, the last read after expiry, refreshing what is asked
for, statuses that need missed stories, how replies are linked, honest comparisons, the anonymized dashboard."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import unittest
from datetime import datetime, timezone

from _helpers import FakeApi, TempHome, no_sleep, story, user, view

import stories as cli
from tgstories import analytics, collector, config, dashboard, db, pace, rules, tables
from tgstories.sender import Sender

T0 = 1_760_000_000
DAY = 86400


def owner_db(premium=True):
    con = db.connect()
    db.set_meta(con, "owner_id", 1)
    db.set_meta(con, "owner_premium", int(premium))
    con.execute("INSERT INTO peers(peer_id,kind,title,username,added_at) VALUES(1,'self','Owner','owner',0)")
    return con


def utc_cfg(**over):
    return config._merge(config.load_config(), {"timezone": "UTC", **over})


def put_rule(con, cfg, trigger="view", **over):
    spec = {"name": "t", "stories": {"mode": "ids", "ids": [100]}, "audience": {"mode": "all"}, "scope": "all",
            "trigger": trigger, "action": {"type": "notify"}, **over}
    n = rules.normalize(spec, cfg)
    cur = con.execute("INSERT INTO rules(name,status,spec,digest,created_at,updated_at) VALUES(?,'shadow',?,?,0,0)",
                      (n["name"], db.dumps(n), rules.digest(n)))
    return con.execute("SELECT * FROM rules WHERE id=?", (cur.lastrowid,)).fetchone()


class ReplyPreview(unittest.TestCase):
    def test_a_reply_rule_previews_replies_not_views(self):
        with TempHome():
            con, cfg = owner_db(), utc_cfg()
            for u in (user(10, "Ann"), user(11, "Bob"), user(12, "Cid")):
                db.upsert_person(con, collector.tg.user_row(u))
            db.upsert_story(con, collector.tg.story_row(story(100, T0), 1))
            for uid in (10, 11):
                db.upsert_view(con, {"peer_id": 1, "story_id": 100, "user_id": uid, "viewed_at": T0 + 60})
            con.execute("INSERT INTO story_replies(peer_id,story_id,user_id,msg_id,at,length) VALUES(1,100,11,5,?,3)",
                        (T0 + 90,))
            con.execute("INSERT INTO story_replies(peer_id,story_id,user_id,msg_id,at,length) VALUES(1,100,11,6,?,3)",
                        (T0 + 95,))
            # Cid replied, but his view never made it into a list (incognito, or not read yet)
            con.execute("INSERT INTO story_replies(peer_id,story_id,user_id,msg_id,at,length) VALUES(1,100,12,7,?,3)",
                        (T0 + 99,))
            sim = rules.simulate(con, cfg, put_rule(con, cfg, "reply"))
            self.assertEqual(sorted(sim["would_send"]), [11, 12])      # Ann only viewed
            self.assertEqual((sim["events"], sim["event"]), (2, "replies"))
            sim = rules.simulate(con, cfg, put_rule(con, cfg, "view"))
            self.assertEqual(sorted(sim["would_send"]), [10, 11])

    def test_people_outside_the_audience_are_counted_once(self):
        with TempHome():
            con, cfg = owner_db(), utc_cfg()
            db.upsert_person(con, collector.tg.user_row(user(10, "Ann", username="ann")))
            db.upsert_person(con, collector.tg.user_row(user(11, "Bob", username="bob")))
            for sid in (100, 101):
                db.upsert_story(con, collector.tg.story_row(story(sid, T0 + sid), 1))
                for uid in (10, 11):
                    db.upsert_view(con, {"peer_id": 1, "story_id": sid, "user_id": uid, "viewed_at": T0 + sid + 60})
            rule = put_rule(con, cfg, stories={"mode": "ids", "ids": [100, 101]},
                            audience={"mode": "users", "users": ["@ann"]})
            sim = rules.simulate(con, cfg, rule)
            self.assertEqual(sim["would_send"], [10])
            self.assertEqual(sim["excluded"], {"not_in_audience": 1})


class FinalRead(unittest.TestCase):
    def setup(self, premium=True, expired_ago=3600):
        con = owner_db(premium)
        t = db.now() - expired_ago - 24 * 3600
        st = {100: (story(100, t, expire=t + 24 * 3600, pinned=False), [view(10, t + 60)])}
        api = FakeApi(st, [user(10, "Ann"), user(11, "Bob")])
        events = []
        col = collector.Collector(api, con, utc_cfg(), 1, on_view=lambda k, r: events.append((k, r.get("user_id"))),
                                  sleep=no_sleep)
        asyncio.run(col.refresh_active())
        asyncio.run(col.poll_counters([100]))             # the last regular poll before the expiry
        return con, st, api, col, events, t

    def test_views_between_the_last_poll_and_the_expiry_are_caught(self):
        with TempHome():
            con, st, api, col, events, t = self.setup()
            self.assertNotIn(100, col.active_ids())       # expired: no regular poll reads it any more
            self.assertEqual(col.finalize_due(), [100])
            st[100][1].insert(0, view(11, t + 24 * 3600 - 30))   # came 30 s before the expiry
            found = asyncio.run(col.finalize_expired())
            self.assertEqual(found, 1)
            self.assertIn(("view", 11), events)          # the rules see it like any other view
            self.assertIsNotNone(con.execute("SELECT finalized_at FROM stories").fetchone()[0])
            api.calls.clear()
            self.assertEqual(asyncio.run(col.finalize_expired()), 0)
            self.assertEqual(api.calls, [])              # once per story

    def test_a_failed_read_is_retried_later_and_does_not_mark(self):
        with TempHome():
            con, st, api, col, events, t = self.setup()

            async def broken(*a, **k):
                raise ConnectionError("offline")
            api.views_list = broken
            asyncio.run(col.finalize_expired())
            self.assertIsNone(con.execute("SELECT finalized_at FROM stories").fetchone()[0])
            self.assertEqual(col.finalize_due(), [])      # backs off for 30 minutes
            col._final_retry_at.clear()
            self.assertEqual(col.finalize_due(), [100])

    def test_without_premium_the_list_is_gone_after_a_day(self):
        with TempHome():
            con, st, api, col, events, t = self.setup(premium=False, expired_ago=30 * 3600)
            self.assertEqual(col.finalize_due(), [])
        with TempHome():
            con, st, api, col, events, t = self.setup(premium=True, expired_ago=30 * 3600)
            self.assertEqual(col.finalize_due(), [100])

    def test_an_empty_answer_later_does_not_erase_a_list_once_read(self):
        with TempHome():
            con, st, api, col, events, t = self.setup()
            async def gone(peer, story_id, offset="", limit=100):    # the counter stays, the list is not given
                return type("R", (), {"count": 0, "views_count": 5, "reactions_count": 0, "views": [],
                                      "users": [], "next_offset": ""})()
            api.views_list = gone
            asyncio.run(col.fetch_views(100, full=True))
            self.assertEqual(con.execute("SELECT list_available FROM stories").fetchone()[0], 1)
            self.assertEqual(analytics.story_at(con, 1, 100, 24), 1)

    def test_the_pass_from_the_command_line_finalizes_too(self):
        with TempHome():
            con, st, api, col, events, t = self.setup()
            asyncio.run(collector.sync(api, con, utc_cfg(), 1))
            self.assertIsNotNone(con.execute("SELECT finalized_at FROM stories").fetchone()[0])

    def test_migration_marks_lists_read_after_the_expiry(self):
        with TempHome():
            path = config.db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            import sqlite3
            old = sqlite3.connect(path)
            old.executescript(db.SCHEMA_V1 + db.SCHEMA_V2 + db.SCHEMA_V3 + "PRAGMA user_version=3;")
            old.execute("INSERT INTO stories(peer_id,story_id,posted_at,expire_at,list_synced) VALUES(1,1,0,100,500)")
            old.execute("INSERT INTO stories(peer_id,story_id,posted_at,expire_at,list_synced) VALUES(1,2,0,100,50)")
            old.execute("INSERT INTO deliveries(rule_id,user_id,status,replied_at) VALUES(1,10,'sent',7)")
            old.commit()
            old.close()
            con = db.connect()
            self.assertEqual(dict(con.execute("SELECT story_id, finalized_at FROM stories").fetchall()),
                             {1: 500, 2: None})
            self.assertEqual(con.execute("SELECT reply_kind FROM deliveries").fetchone()[0], "after")


class RefreshWhatIsShown(unittest.TestCase):
    def test_plan_reads_the_story_or_channel_asked_for(self):
        with TempHome():
            con = owner_db()
            now = db.now()
            db.set_state(con, "last_poll_at", now)
            db.upsert_story(con, collector.tg.story_row(story(100, now - 10 * DAY), 1))     # expired long ago
            db.upsert_story(con, collector.tg.story_row(story(101, now - 3600), 1))         # active
            con.execute("UPDATE stories SET last_synced=?", (now - 2 * DAY,))
            self.assertEqual(pace.plan(con, ("story", "100")), {"general": False, "story": 100, "channel": None})
            self.assertEqual(pace.plan(con, ("story", "101"))["story"], None)    # the general pass reads it
            self.assertEqual(pace.plan(con, ("story", "555"))["story"], 555)     # not in the database yet
            self.assertEqual(pace.plan(con, ("all", None)), {"general": False, "story": None, "channel": None})
            con.execute("INSERT INTO peers(peer_id,kind,title,username,added_at) VALUES(-5,'channel','C','chan',0)")
            self.assertEqual(pace.plan(con, ("channel", "@chan"))["channel"], "@chan")
            db.set_state(con, "channel_polled_at:-5", now)
            self.assertFalse(any(pace.plan(con, ("channel", "@chan")).values()))

    def test_target_of_the_command(self):
        p = cli.build_parser()
        target = lambda *a: cli.target_of(p.parse_args(list(a)))  # noqa: E731
        self.assertEqual(target("table", "story", "221"), ("story", "221"))
        self.assertEqual(target("table", "story"), ("story", "last"))
        self.assertEqual(target("table", "channel", "--peer", "@x"), ("channel", "@x"))
        self.assertEqual(target("table", "summary"), ("all", None))
        self.assertEqual(target("dashboard"), ("all", None))

    def test_an_old_story_is_read_by_itself_even_if_missing(self):
        with TempHome():
            con = owner_db()
            t = db.now() - 10 * DAY
            st = {100: (story(100, t), [view(10, t + 60), view(11, t + 30)])}
            api = FakeApi(st, [user(10, "Ann"), user(11, "Bob")])

            async def by_id(peer, ids):
                api.calls.append(("by_id", tuple(ids)))
                return type("R", (), {"stories": [st[i][0] for i in ids if i in st], "users": api.users})()
            api.by_id = by_id
            found = asyncio.run(collector.refresh_story(api, con, utc_cfg(), 1, 100))
            self.assertEqual(found, 2)
            self.assertIn(("by_id", (100,)), api.calls)
            self.assertNotIn(("peer_stories",), api.calls)     # not the general pass

    def test_a_failed_refresh_names_the_time_of_that_story(self):
        with TempHome() as home:
            (home / "telegram-stories.json").write_text(json.dumps({"timezone": "UTC"}), encoding="utf-8")
            con = owner_db()
            when = db.now() - 3 * DAY
            db.upsert_story(con, collector.tg.story_row(story(100, when - DAY), 1))
            con.execute("UPDATE stories SET last_synced=?", (when,))
            db.set_state(con, "last_poll_at", db.now())

            async def broken(c, **todo):
                raise ConnectionError("offline")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertFalse(cli.freshen(config.load_config(), broken, target=("story", "100")))
            self.assertIn(datetime.fromtimestamp(when).strftime("%d.%m %H:%M"), err.getvalue())


class Statuses(unittest.TestCase):
    def test_cooling_needs_missed_stories_and_a_pause_is_not_people_leaving(self):
        with TempHome():
            con = owner_db()
            now = T0 + 100 * DAY
            for i in range(6):                       # six stories, Ann saw them all; then nothing was posted
                db.upsert_story(con, collector.tg.story_row(story(100 + i, T0 + i * DAY), 1))
                db.upsert_view(con, {"peer_id": 1, "story_id": 100 + i, "user_id": 10,
                                     "viewed_at": T0 + i * DAY + 60})
            con.execute("UPDATE stories SET list_available=1")
            ann = {p["user_id"]: p for p in analytics.people(con, 1, now=now)}[10]
            self.assertNotEqual(ann["status"], "cooling")
            self.assertEqual(ann["missed"], 0)
            sm = analytics.summary(con, 1, now - 30 * DAY, now)
            self.assertIsNone(sm["lost_viewers"])    # nothing posted in the period: unknown, not zero
            for i in range(2):                       # two new stories she skipped
                db.upsert_story(con, collector.tg.story_row(story(200 + i, T0 + (80 + i) * DAY), 1))
            con.execute("UPDATE stories SET list_available=1")
            ann = {p["user_id"]: p for p in analytics.people(con, 1, now=now)}[10]
            self.assertEqual((ann["status"], ann["missed"]), ("cooling", 2))


class ReplyLinks(unittest.TestCase):
    def test_direct_reply_and_wrote_after(self):
        with TempHome():
            con = owner_db()
            now = db.now()
            for rule_id, sent, msg in ((1, now - 3 * DAY, 501), (2, now - DAY, 502)):
                con.execute("INSERT INTO deliveries(rule_id,user_id,status,sent_at,msg_id) VALUES(?,10,'sent',?,?)",
                            (rule_id, sent, msg))
            s = Sender(None, con, utc_cfg())
            self.assertEqual(s.on_reply(10), "after")             # only the latest message gets it
            kinds = dict(con.execute("SELECT rule_id, reply_kind FROM deliveries").fetchall())
            self.assertEqual(kinds, {1: None, 2: "after"})
            self.assertIsNone(s.on_reply(10))                      # already linked
            self.assertEqual(s.on_reply(10, reply_to=501), "direct")   # a reply to the older message itself
            self.assertEqual(s.on_reply(10, reply_to=502), "direct")   # upgrades "after"
            kinds = dict(con.execute("SELECT rule_id, reply_kind FROM deliveries").fetchall())
            self.assertEqual(kinds, {1: "direct", 2: "direct"})

    def test_a_message_long_after_is_not_a_reply(self):
        with TempHome():
            con = owner_db()
            con.execute("INSERT INTO deliveries(rule_id,user_id,status,sent_at,msg_id) VALUES(1,10,'sent',?,1)",
                        (db.now() - 8 * DAY,))
            self.assertIsNone(Sender(None, con, utc_cfg()).on_reply(10))
            self.assertIsNone(con.execute("SELECT replied_at FROM deliveries").fetchone()[0])


class HonestNumbers(unittest.TestCase):
    def test_best_hours_compare_the_first_24_hours(self):
        with TempHome():
            con = owner_db()
            tz = timezone.utc
            base = datetime(2025, 1, 6, 10, tzinfo=tz).timestamp()
            for i, early in enumerate((5, 6, 7)):     # posted at 10:00 — and one gets 50 more views a week later
                t = int(base + i * DAY)
                db.upsert_story(con, collector.tg.story_row(story(100 + i, t), 1))
                for k in range(early):
                    db.upsert_view(con, {"peer_id": 1, "story_id": 100 + i, "user_id": 1000 + k, "viewed_at": t + 60})
                if i == 0:
                    for k in range(50):
                        db.upsert_view(con, {"peer_id": 1, "story_id": 100, "user_id": 2000 + k,
                                             "viewed_at": t + 7 * DAY})
            con.execute("UPDATE stories SET list_available=1, list_synced=?", (db.now(),))
            h = analytics.hours(con, 1, 0, db.now() + 1, tz)
            self.assertEqual(h["best_post_hours"], [(10, 6, 3, 5, 7)])      # the late 50 do not count
            partial = int(base + 3 * DAY)               # an old import: 2 listed of 100, both dated late
            db.upsert_story(con, collector.tg.story_row(story(400, partial), 1))
            for k in range(2):
                db.upsert_view(con, {"peer_id": 1, "story_id": 400, "user_id": 3000 + k, "viewed_at": partial + 40 * DAY})
            con.execute("UPDATE stories SET views=100 WHERE story_id=400")
            con.execute("UPDATE stories SET list_available=1, list_synced=? WHERE story_id=400", (db.now(),))
            self.assertEqual(analytics.hours(con, 1, 0, db.now() + 1, tz)["best_post_hours"], [(10, 6, 3, 5, 7)])
            young = int(db.now() - 3600)
            db.upsert_story(con, collector.tg.story_row(story(300, young), 1))
            con.execute("UPDATE stories SET list_available=1, list_synced=? WHERE story_id=300", (db.now(),))
            self.assertEqual(analytics.hours(con, 1, 0, db.now() + 1, tz)["best_post_hours"], [(10, 6, 3, 5, 7)])

    def test_tables_say_how_complete_the_data_is(self):
        with TempHome():
            con, cfg = owner_db(), utc_cfg()
            t = db.now() - 3 * DAY
            st = {100: (story(100, t, expire=t + DAY, pinned=False), [view(10, t + 60), view(11, t + 30)])}
            api = FakeApi(st, [user(10, "Ann"), user(11, "Bob")])
            col = collector.Collector(api, con, cfg, 1, sleep=no_sleep)
            asyncio.run(col.refresh_active())
            asyncio.run(col.poll_counters([100]))
            con.execute("UPDATE stories SET views=5")
            md = tables.story_table(con, cfg, 1, "100").to_md()
            self.assertIn("2 of 5 in the list", md)
            self.assertIn("final read after expiry pending", md)
            asyncio.run(col.finalize_expired())
            self.assertIn("final read after expiry", tables.story_table(con, cfg, 1, "100").to_md())
            self.assertNotIn("pending", tables.story_table(con, cfg, 1, "100").to_md())
            summary = tables.summary_table(con, cfg, 1, 0, db.now() + 1).to_md()
            self.assertIn("first seen in the collected history, which starts", summary)
            self.assertIn("Stopped viewing: —", summary)


class AnonymizedDashboard(unittest.TestCase):
    def test_no_viewer_is_in_the_file(self):
        with TempHome():
            con, cfg = owner_db(), utc_cfg()
            t = T0
            people = [user(987654300 + i, f"Secretname{i}", "Hidden", f"secret_nick_{i}", contact=True)
                      for i in range(5)]
            st = {100 + k: (story(100 + k, t + k * DAY, caption=f"caption {k}"),
                            [view(987654300 + i, t + k * DAY + 60 + i) for i in range(5)]) for k in range(4)}
            col = collector.Collector(FakeApi(st, people), con, cfg, 1, sleep=no_sleep)
            asyncio.run(col.backfill(thumbs=False, progress=None))
            rule = put_rule(con, cfg, name="Write to Secretname2", audience={"mode": "users", "users": ["@secret_nick_1"]})
            self.assertTrue(rule)
            private = dashboard.render(con, cfg, dashboard.collect(con, cfg))
            anon = dashboard.render(con, cfg, dashboard.collect(con, cfg), anonymized=True)
            self.assertIn("Secretname1", private)            # the control: the private page has them
            self.assertIn("Write to Secretname2", private)
            for i in range(5):
                for needle in (f"Secretname{i}", f"secret_nick_{i}", str(987654300 + i)):
                    self.assertNotIn(needle, anon)
            self.assertNotIn("Hidden", anon)
            self.assertIn("Anonymized copy", anon)
            self.assertIn("caption 3", anon)                  # your own captions stay
            data = json.loads(re.search(r'<script type="application/json" id="dash-data">(.*?)</script>', anon,
                                        re.S).group(1))
            self.assertEqual((data["people"], data["views"], data["anon"]), ([], {}, True))
            path = dashboard.build(cfg, con=con, anonymized=True)
            self.assertEqual(path.name, "dashboard-anon.html")


class Login(unittest.TestCase):
    def test_no_mode_takes_a_login_code_from_a_chat(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["login", "finish", "--code", "12345"])
        from tgstories import login
        self.assertFalse(hasattr(login, "finish"))


if __name__ == "__main__":
    unittest.main()
