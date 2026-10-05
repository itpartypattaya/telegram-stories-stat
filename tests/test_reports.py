"""Pulse, digests and notification routing — synthetic data, no network."""
from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timezone

from _helpers import FakeApi, TempHome, no_sleep, story, user, view

from tgstories import collector, config, db, notify, reports

T0 = 1_760_000_000


def history(n_stories=6, base_views=10):
    """n stories a day apart; story k gets base_views + k viewers within the first hour."""
    users = [user(100 + i, f"U{i}", None, f"u{i}", contact=True) for i in range(40)]
    st = {}
    for k in range(n_stories):
        t = T0 + k * 86400
        vs = [view(100 + i, t + 600 + i * 30) for i in range(base_views + k)]
        st[1000 + k] = (story(1000 + k, t, caption=f"s{k}"), list(reversed(vs)))
    return st, users


class PulseDigestTest(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.home.__enter__()
        self.con = db.connect()
        db.set_meta(self.con, "owner_id", 1)
        st, users = history()
        self.cfg = config._merge(config.load_config(), {"timezone": "UTC"})
        col = collector.Collector(FakeApi(st, users), self.con, self.cfg, 1, sleep=no_sleep)
        asyncio.run(col.backfill(thumbs=False, progress=None))

    def tearDown(self):
        self.con.close()
        self.home.__exit__(None, None, None)

    def test_pulse_compares_with_earlier_stories(self):
        p = reports.pulse(self.con, self.cfg, 1, 1005)
        self.assertEqual(p["views"], 15)                   # 10 + 5 viewers, all in the first hour
        self.assertEqual(p["baseline"], 12)                # median of 10..14
        text = reports.pulse_text(self.con, self.cfg, "1005", peer_id=1)
        self.assertIn("📈", text)

    def test_due_pulses_window(self):
        posted = T0 + 5 * 86400
        self.assertEqual(reports.due_pulses(self.con, self.cfg, 1, now_ts=posted + 3600), [])
        self.assertEqual(reports.due_pulses(self.con, self.cfg, 1, now_ts=posted + 2 * 3600 + 60), [1005])
        self.assertEqual(reports.due_pulses(self.con, self.cfg, 1, now_ts=posted + 12 * 3600), [])  # too late

    def test_digest_once_per_week(self):
        sunday_evening = int(datetime(2026, 10, 11, 20, 30, tzinfo=timezone.utc).timestamp())
        due = reports.due_digests(self.con, self.cfg, now_ts=sunday_evening)
        self.assertEqual([p for _, p in due], ["7d"])
        reports.mark_sent(self.con, due[0][0])
        self.assertEqual(reports.due_digests(self.con, self.cfg, now_ts=sunday_evening + 600), [])
        before = int(datetime(2026, 10, 11, 19, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(reports.due_digests(self.con, self.cfg, now_ts=before - 7 * 86400), [])

    def test_digest_text_is_a_table(self):
        text = reports.digest_text(self.con, self.cfg, period="all", peer_id=1)
        self.assertIn("|---:|", text)
        self.assertIn("1005", text)


class NotifyRoutingTest(unittest.TestCase):
    def test_mode(self):
        with TempHome() as home:
            cfg = config.load_config()
            self.assertEqual(notify.mode(cfg), "saved")          # no bot known
            (home / ".env").write_text("TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_HOME_CHANNEL=-100123\n", encoding="utf-8")
            self.assertEqual(notify.mode(cfg), "bot")
            self.assertEqual(notify.mode(config._merge(cfg, {"notify": {"via": "none"}})), "none")
            token, chat, thread = notify._target(config._merge(cfg, {"notify": {"chat_id": "-1", "thread_id": "7"}}))
            self.assertEqual((chat, thread), ("-1", "7"))


if __name__ == "__main__":
    unittest.main()
