"""1.5.1: a deleted story, views found late, stories posted while the service was down, raw bytes in the logs,
honest baselines and the real final-read window."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import unittest
from types import SimpleNamespace

from _helpers import SKILL, FakeApi, TempHome, no_sleep, story, user, view

from tgstories import analytics, collector, config, db, pace, reports, rules, tables, tg
from tgstories.i18n import t

DAY = 86400


def owner_db(premium=True):
    con = db.connect()
    db.set_meta(con, "owner_id", 1)
    db.set_meta(con, "owner_premium", int(premium))
    con.execute("INSERT INTO peers(peer_id,kind,title,username,added_at) VALUES(1,'self','Owner','owner',0)")
    return con


def utc_cfg(**over):
    return config._merge(config.load_config(), {"timezone": "UTC", **over})


def put_rule(con, cfg, status="active", activated_at=None, **over):
    spec = {"name": "t", "stories": {"mode": "all"}, "audience": {"mode": "all"}, "scope": "all",
            "trigger": "view", "action": {"type": "notify"}, **over}
    n = rules.normalize(spec, cfg)
    cur = con.execute("INSERT INTO rules(name,status,spec,digest,created_at,updated_at,activated_at) "
                      "VALUES(?,?,?,?,0,0,?)", (n["name"], status, db.dumps(n), rules.digest(n), activated_at))
    return cur.lastrowid


def deliveries(con, rule_id):
    return con.execute("SELECT COUNT(*) FROM deliveries WHERE rule_id=?", (rule_id,)).fetchone()[0]


class DeletedStory(unittest.TestCase):
    def _collector(self, con, api):
        return collector.Collector(api, con, utc_cfg(), 1, sleep=no_sleep)

    def test_a_deleted_active_story_leaves_the_poll(self):
        with TempHome():
            con, now = owner_db(), db.now()
            api = FakeApi({100: (story(100, now - 3600), [view(10, now - 60)])}, [user(10)])
            col = self._collector(con, api)
            asyncio.run(col.refresh_active())
            self.assertEqual(col.active_ids(), [100])
            del api.stories[100]          # deleted in Telegram: gone from the active list and from by_id
            asyncio.run(col.refresh_active())
            self.assertEqual(con.execute("SELECT deleted FROM stories WHERE story_id=100").fetchone()[0], 1)
            self.assertEqual(col.active_ids(), [])
            self.assertIn(("by_id", (100,)), api.calls)

    def test_a_story_found_by_id_is_kept(self):
        with TempHome():
            con, now = owner_db(), db.now()
            api = FakeApi({100: (story(100, now - 3600), [])}, [])
            col = self._collector(con, api)
            asyncio.run(col.refresh_active())

            async def no_active(peer):
                return SimpleNamespace(stories=SimpleNamespace(stories=[]), users=[])
            api.peer_stories = no_active
            asyncio.run(col.refresh_active())
            self.assertEqual(col.active_ids(), [100])

    def test_no_answer_marks_nothing(self):
        with TempHome():
            con, now = owner_db(), db.now()
            api = FakeApi({100: (story(100, now - 3600), [])}, [])
            col = self._collector(con, api)
            asyncio.run(col.refresh_active())
            del api.stories[100]

            async def broken(peer, ids):
                raise ConnectionError("network")
            api.by_id = broken
            asyncio.run(col.refresh_active())
            self.assertEqual(col.active_ids(), [100])

    def test_a_zero_counter_never_overwrites_the_views(self):
        with TempHome():
            con, now = owner_db(), db.now()
            api = FakeApi({100: (story(100, now - 3600), [])}, [])   # Telegram answers 0 for a deleted story
            db.upsert_story(con, {**tg.story_row(story(100, now - 3600), 1), "views": 50})
            con.execute("UPDATE stories SET list_synced=?, listed_views=50, list_available=1", (now,))
            col = self._collector(con, api)
            col._last_full[100] = now      # the half-hourly full read is not what this is about
            asyncio.run(col.poll_counters([100]))
            self.assertEqual(con.execute("SELECT views FROM stories WHERE story_id=100").fetchone()[0], 50)
            self.assertFalse([c for c in api.calls if c[0] == "views_list"])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM story_snapshots").fetchone()[0], 0)


class LateEvents(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.home.__enter__()
        self.con, self.now = owner_db(), db.now()
        db.upsert_person(self.con, tg.user_row(user(10, "Ann")))
        db.upsert_story(self.con, tg.story_row(story(100, self.now - 3 * DAY), 1))

    def tearDown(self):
        self.home.__exit__(None, None, None)

    def ev(self, at):
        return {"peer_id": 1, "story_id": 100, "user_id": 10, "viewed_at": at}

    def test_a_view_found_days_later_wakes_no_rule(self):
        cfg = utc_cfg()
        rid = put_rule(self.con, cfg, activated_at=self.now - 3 * DAY - 60)
        self.assertEqual(rules.Engine(self.con, cfg).on_event("view", self.ev(self.now - 2 * DAY)), 0)
        self.assertEqual(deliveries(self.con, rid), 0)
        self.assertEqual(rules.Engine(self.con, cfg).on_event("view", self.ev(self.now - 60)), 1)

    def test_a_view_from_before_the_activation_wakes_no_active_rule(self):
        cfg = utc_cfg()
        put_rule(self.con, cfg, activated_at=self.now - 600)
        self.assertEqual(rules.Engine(self.con, cfg).on_event("view", self.ev(self.now - 1200)), 0)
        self.assertEqual(rules.Engine(self.con, cfg).on_event("view", self.ev(self.now - 300)), 1)

    def test_a_shadow_rule_has_no_activation_but_the_same_age_limit(self):
        cfg = utc_cfg()
        put_rule(self.con, cfg, status="shadow")
        self.assertEqual(rules.Engine(self.con, cfg).on_event("view", self.ev(self.now - 2 * DAY)), 0)
        self.assertEqual(rules.Engine(self.con, cfg).on_event("view", self.ev(self.now - 1200)), 1)

    def test_zero_switches_the_age_limit_off(self):
        cfg = utc_cfg(autoresponder={"max_event_age_h": 0})
        put_rule(self.con, cfg, status="shadow")
        self.assertEqual(rules.Engine(self.con, cfg).on_event("view", self.ev(self.now - 2 * DAY)), 1)


class ArchiveCatchUp(unittest.TestCase):
    def test_a_story_posted_during_a_downtime_is_found_read_and_wakes_nothing(self):
        with TempHome():
            con, now, cfg = owner_db(), db.now(), utc_cfg()
            db.upsert_story(con, {**tg.story_row(story(100, now - 5 * DAY, expire=now - 4 * DAY), 1), "views": 77})
            con.execute("UPDATE stories SET finalized_at=? WHERE story_id=100", (now - 4 * DAY,))
            api = FakeApi({100: (story(100, now - 5 * DAY, expire=now - 4 * DAY, views=3), []),
                           101: (story(101, now - 3 * DAY, expire=now - 2 * DAY),
                                 [view(10, now - 3 * DAY + 60, "❤")])}, [user(10)])
            rid = put_rule(con, cfg, activated_at=now - 30 * DAY)
            engine = rules.Engine(con, cfg)
            col = collector.Collector(api, con, cfg, 1, on_view=engine.on_event, sleep=no_sleep)
            self.assertEqual(asyncio.run(col.catch_up_archive()), [101])
            self.assertEqual(con.execute("SELECT views FROM stories WHERE story_id=100").fetchone()[0], 77)
            self.assertEqual(col.finalize_due(), [101])
            asyncio.run(col.finalize_expired())
            self.assertEqual(con.execute("SELECT viewers_listed FROM stories WHERE story_id=101").fetchone()[0], 1)
            self.assertEqual(deliveries(con, rid), 0)     # a view of three days ago: no message now
            self.assertEqual(asyncio.run(col.catch_up_archive()), [])


class RawBytesInLogs(unittest.TestCase):
    RAW = "Could not find a matching Constructor ID for the TLObject. Remaining bytes: b'\\x07jane_doe\\x0b79991234567'"

    def test_the_message_is_cut_and_explained(self):
        rec = logging.LogRecord("telethon", logging.WARNING, "", 0, "Cannot get difference: %s", (self.RAW,), None)
        tg.RedactRawBytes().filter(rec)
        msg = rec.getMessage()
        self.assertNotIn("jane_doe", msg)
        self.assertNotIn("7999", msg)
        self.assertIn("update Telethon", msg)

    def test_the_traceback_is_cut(self):
        try:
            raise ValueError(self.RAW)
        except ValueError:
            rec = logging.LogRecord("telethon", logging.ERROR, "", 0, "Unhandled error", (), sys.exc_info())
        tg.RedactRawBytes().filter(rec)
        text = logging.Formatter().format(rec)
        self.assertIn("Unhandled error", text)
        self.assertIn("ValueError", text)
        self.assertNotIn("jane_doe", text)

    def test_installed_once_on_every_handler(self):
        root = logging.getLogger()
        h = logging.StreamHandler()
        root.addHandler(h)
        try:
            tg.redact_logs()
            tg.redact_logs()
            for handler in (h, logging.lastResort):
                self.assertEqual(sum(isinstance(f, tg.RedactRawBytes) for f in handler.filters), 1)
        finally:
            root.removeHandler(h)

    def test_the_installer_wants_the_layer_the_service_needs(self):
        spec = importlib.util.spec_from_file_location("install_mod", SKILL / "scripts" / "install.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIn(f"LAYER >= {tg.MIN_LAYER}", mod.TELETHON_PROBE)
        self.assertIn(">=1.45", mod.DEP_TELETHON)


class HonestNumbers(unittest.TestCase):
    def _story_with_list(self, con, sid, posted, views, listed):
        db.upsert_story(con, {**tg.story_row(story(sid, posted), 1), "views": views})
        for u in range(listed):
            db.upsert_view(con, {"peer_id": 1, "story_id": sid, "user_id": 1000 + u, "viewed_at": posted + 60})
        db.refresh_story_listing(con, 1, sid)
        con.execute("UPDATE stories SET list_available=1, list_synced=? WHERE story_id=?", (posted + DAY, sid))

    def test_the_same_age_baseline_skips_partial_lists(self):
        with TempHome():
            con, now = owner_db(), db.now()
            self._story_with_list(con, 1, now - 10 * DAY, 10, 10)
            self._story_with_list(con, 2, now - 9 * DAY, 12, 12)
            self._story_with_list(con, 3, now - 8 * DAY, 300, 3)     # an old import: a third of the list
            self.assertEqual(analytics.baseline_at(con, 1, now, 24), 11)

    def test_the_pending_final_read_follows_the_real_window(self):
        with TempHome():
            cfg, now = utc_cfg(), db.now()
            s = {"last_synced": now, "list_available": 1, "views": 5, "expire_at": now - 21 * 3600,
                 "peer_id": 1, "owner_id": 1}
            con = owner_db(premium=False)
            line = tables.story_quality(s, cfg, config.get_tz(cfg), 5, pace.final_window(con, cfg, 1))
            self.assertIn(t(cfg, "final_none"), line)      # without Premium the list is gone after 24 h
            db.set_meta(con, "owner_premium", 1)
            line = tables.story_quality(s, cfg, config.get_tz(cfg), 5, pace.final_window(con, cfg, 1))
            self.assertIn(t(cfg, "final_due"), line)

    def test_no_pulse_without_a_list(self):
        with TempHome():
            con, now, cfg = owner_db(), db.now(), utc_cfg()
            db.upsert_story(con, tg.story_row(story(100, now - 3 * 3600), 1))
            self.assertEqual(reports.due_pulses(con, cfg, 1), [100])
            sent = []

            async def alert(md):
                sent.append(md)
                return True
            asyncio.run(reports.send_due(con, cfg, 1, alert))
            self.assertFalse([m for m in sent if t(cfg, "no_data") in m])
            self.assertEqual(reports.due_pulses(con, cfg, 1), [])   # handled: not re-read every minute


if __name__ == "__main__":
    unittest.main()
