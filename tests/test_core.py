"""Config, env file, database, collector and analytics on synthetic data (no network, no Telethon)."""
from __future__ import annotations

import asyncio
import json
import os
import unittest

from _helpers import FakeApi, TempHome, no_sleep, story, user, view

from tgstories import analytics, collector, config, db

T0 = 1_760_000_000  # a fixed moment in time


def fresh_db():
    con = db.connect()
    db.set_meta(con, "owner_id", 1)
    return con


class EnvFileTest(unittest.TestCase):
    def test_write_keeps_other_lines_and_mode(self):
        with TempHome() as home:
            env = home / ".env"
            env.write_text("# comment\nA=1\nexport B='two'\nC=3\n", encoding="utf-8")
            config.write_env_value("B", "new")
            config.write_env_value("D", "4")
            text = env.read_text(encoding="utf-8")
            self.assertEqual(text, "# comment\nA=1\nB=new\nC=3\nD=4\n")
            if os.name == "posix":
                self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.read_env_file()["B"], "new")

    def test_secret_prefers_environment(self):
        with TempHome() as home:
            (home / ".env").write_text("X_TEST_SECRET=file\n", encoding="utf-8")
            self.assertEqual(config.secret("X_TEST_SECRET"), "file")
            os.environ["X_TEST_SECRET"] = "env"
            try:
                self.assertEqual(config.secret("X_TEST_SECRET"), "env")
            finally:
                os.environ.pop("X_TEST_SECRET")

    def test_config_merge_and_comments(self):
        with TempHome() as home:
            (home / "telegram-stories.json").write_text(json.dumps(
                {"_about": "x", "locale": "ru", "autoresponder": {"enabled": True}}), encoding="utf-8")
            cfg = config.load_config()
            self.assertEqual(cfg["locale"], "ru")
            self.assertTrue(cfg["autoresponder"]["enabled"])
            self.assertEqual(cfg["autoresponder"]["default_scope"], "contacts")  # default kept
            self.assertNotIn("_about", cfg)


class DbTest(unittest.TestCase):
    def test_migrate_idempotent(self):
        with TempHome():
            con = db.connect()
            v = con.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(v, db.SCHEMA_VERSION)
            self.assertEqual(db.migrate(con), db.SCHEMA_VERSION)

    def test_people_history_on_rename(self):
        with TempHome():
            con = fresh_db()
            db.upsert_person(con, {"user_id": 5, "first_name": "Ann", "username": "ann"}, ts=T0)
            db.upsert_person(con, {"user_id": 5, "first_name": "Ann", "username": "ann_new"}, ts=T0 + 10)
            row = con.execute("SELECT username FROM people WHERE user_id=5").fetchone()
            self.assertEqual(row[0], "ann_new")
            hist = con.execute("SELECT field, old, new FROM people_history WHERE user_id=5").fetchall()
            self.assertEqual([tuple(h) for h in hist], [("username", "ann", "ann_new")])

    def test_upsert_view_keeps_first_and_latest(self):
        with TempHome():
            con = fresh_db()
            base = {"peer_id": 1, "story_id": 9, "user_id": 5}
            self.assertEqual(db.upsert_view(con, {**base, "viewed_at": T0 + 100}), "new")
            self.assertEqual(db.upsert_view(con, {**base, "viewed_at": T0 + 100}), "same")
            self.assertEqual(db.upsert_view(con, {**base, "viewed_at": T0 + 500, "reaction": "❤"}), "changed")
            r = con.execute("SELECT first_viewed_at, viewed_at, reaction FROM views").fetchone()
            self.assertEqual(tuple(r), (T0 + 100, T0 + 500, "❤"))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM view_events").fetchone()[0], 2)


def model():
    users = [user(10, "Ann", "A", "ann", contact=True, mutual=True), user(11, "Bob", None, None, contact=True),
             user(12, "Cid", None, "cid")]
    s1 = story(100, T0, caption="first")
    s2 = story(101, T0 + 86400, caption="second")
    stories = {
        100: (s1, [view(12, T0 + 7200), view(11, T0 + 3000, "❤"), view(10, T0 + 600)]),
        101: (s2, [view(10, T0 + 86400 + 300)]),
    }
    return stories, users


class CollectorTest(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_backfill_and_incremental(self):
        with TempHome():
            con = fresh_db()
            stories, users = model()
            api = FakeApi(stories, users)
            events = []
            col = collector.Collector(api, con, config.load_config(), 1, on_view=lambda k, r: events.append(k),
                                      sleep=no_sleep)
            res = self.run_async(col.backfill(thumbs=False, progress=None))
            self.assertEqual(res["imported"], 2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM views").fetchone()[0], 4)
            self.assertEqual(con.execute("SELECT username FROM people WHERE user_id=10").fetchone()[0], "ann")
            # resumable: a second run imports nothing
            res2 = self.run_async(col.backfill(thumbs=False, progress=None))
            self.assertEqual((res2["imported"], res2["already"]), (0, 2))
            # a new viewer arrives on story 101 → incremental read stops at the first known row
            stories[101][1].insert(0, view(12, T0 + 86400 + 900))
            api.calls.clear()
            found = self.run_async(col.fetch_views(101))
            self.assertEqual(found, 1)
            self.assertEqual(len([c for c in api.calls if c[0] == "views_list"]), 1)

    def test_poll_counters_reads_list_only_on_growth(self):
        with TempHome():
            con = fresh_db()
            stories, users = model()
            api = FakeApi(stories, users)
            col = collector.Collector(api, con, config.load_config(), 1, sleep=no_sleep)
            for s, _ in stories.values():
                db.upsert_story(con, collector.tg.story_row(s, 1))
            self.run_async(col.poll_counters([100, 101]))
            lists = [c for c in api.calls if c[0] == "views_list"]
            self.assertEqual(len(lists), 2)          # first time: never listed
            api.calls.clear()
            self.run_async(col.poll_counters([100, 101]))
            self.assertFalse([c for c in api.calls if c[0] == "views_list"])  # nothing grew


class AnalyticsTest(unittest.TestCase):
    def test_story_metrics_and_people(self):
        with TempHome():
            con = fresh_db()
            stories, users = model()
            col = collector.Collector(FakeApi(stories, users), con, config.load_config(), 1, sleep=no_sleep)
            asyncio.run(col.backfill(thumbs=False, progress=None))
            m = analytics.story(con, 1, 100)
            self.assertEqual(m["listed"], 3)
            self.assertEqual(m["at"][1], 2)          # views within the first hour
            self.assertEqual(m["new_viewers"], 3)
            self.assertEqual(m["composition"], {"mutual": 1, "contact": 1, "other": 1, "close_friend": 0})
            m2 = analytics.story(con, 1, 101)
            self.assertEqual(m2["new_viewers"], 0)   # Ann has seen story 100 before
            self.assertEqual(analytics.resolve_story(con, 1, "last"), 101)
            self.assertEqual(analytics.resolve_story(con, 1, "-2"), 100)
            ppl = {p["user_id"]: p for p in analytics.people(con, 1, now=T0 + 2 * 86400)}
            self.assertEqual(ppl[10]["seen_total"], 2)
            self.assertEqual(ppl[10]["status"], "new")  # first seen within 14 days
            far = {p["user_id"]: p for p in analytics.people(con, 1, now=T0 + 100 * 86400)}
            self.assertEqual(far[10]["status"], "lost")

    def test_parse_period(self):
        from datetime import timezone
        s, e = analytics.parse_period("7d", None, None, timezone.utc)
        self.assertEqual(e - s, 7 * 86400)
        s, e = analytics.parse_period(None, "2026-01-01", "2026-01-31", timezone.utc)
        self.assertEqual(e - s, 31 * 86400)
        with self.assertRaises(SystemExit):
            analytics.parse_period("week", None, None, timezone.utc)


if __name__ == "__main__":
    unittest.main()
