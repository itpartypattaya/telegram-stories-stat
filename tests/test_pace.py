"""The pace: every minute while a rule is active, hourly otherwise; what keeps working at the hourly pace."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import time
import unittest
from unittest import mock

from _helpers import FakeApi, TempHome, story, user, view

import daemon
import stories as cli
from tgstories import collector, config, db, doctor, pace, reports


def cfg_with(home, **poll):
    data = {"timezone": "UTC", "poll": poll} if poll else {"timezone": "UTC"}
    (home / "telegram-stories.json").write_text(json.dumps(data), encoding="utf-8")
    return config.load_config()


def add_rule(con, status="active"):
    spec = {"name": "n", "stories": {"mode": "all"}, "audience": {"mode": "all"}, "scope": "all",
            "trigger": "view", "action": {"type": "notify"}}
    con.execute("INSERT INTO rules(name,status,spec,digest) VALUES('n',?,?,'d')", (status, json.dumps(spec)))


class Pace(unittest.TestCase):
    def test_hourly_without_an_active_rule_minutes_with_one(self):
        with TempHome() as home:
            cfg, con = cfg_with(home), db.connect()
            self.assertEqual(pace.interval(con, cfg, 60), 3600)
            self.assertEqual(pace.interval(con, cfg, 300), 3600)
            self.assertEqual(pace.interval(con, cfg, 3600), 3600)
            add_rule(con, "shadow")                       # a rule in test mode sends nothing: still hourly
            self.assertEqual(pace.interval(con, cfg, 60), 3600)
            add_rule(con, "active")
            self.assertEqual(pace.interval(con, cfg, 60), 60)
            self.assertIn("every minute", doctor.status_text(con, cfg))

    def test_idle_zero_keeps_the_minute_pace(self):
        with TempHome() as home:
            cfg, con = cfg_with(home, idle_s=0), db.connect()
            self.assertEqual(pace.interval(con, cfg, 60), 60)

    def test_doctor_accepts_an_hour_old_poll_only_at_the_hourly_pace(self):
        with TempHome() as home:
            cfg, con = cfg_with(home), db.connect()
            self.assertTrue(pace.poll_age_ok(con, cfg, 50 * 60))
            self.assertIn("every 60 min", doctor.status_text(con, cfg))
            add_rule(con)
            self.assertFalse(pace.poll_age_ok(con, cfg, 50 * 60))
            self.assertTrue(pace.poll_age_ok(con, cfg, 5 * 60))

    def test_activating_a_rule_speeds_up_within_a_tick(self):
        async def scenario():
            stop, runs, mode = asyncio.Event(), [], {"iv": 100.0}

            async def fn():
                runs.append(time.monotonic())

            task = asyncio.create_task(daemon.every(lambda: mode["iv"], fn, "t", stop, tick=0.02))
            await asyncio.sleep(0.1)
            first = len(runs)                             # ran once, now in a long wait
            mode["iv"] = 0.03                             # a rule was activated
            await asyncio.sleep(0.25)
            stop.set()
            await task
            return first, len(runs)

        first, total = asyncio.run(scenario())
        self.assertEqual(first, 1)
        self.assertGreaterEqual(total, 4)


class AtTheHourlyPace(unittest.TestCase):
    def test_a_pulse_reads_its_story_again_before_it_goes(self):
        with TempHome() as home:
            cfg, con = cfg_with(home), db.connect()
            db.set_meta(con, "owner_id", 1)
            db.upsert_story(con, collector.tg.story_row(story(5, db.now() - int(2.5 * 3600)), 1))
            con.execute("UPDATE stories SET list_available=1, list_synced=?", (db.now(),))
            calls = []

            async def refresh(ids):
                calls.append(("refresh", ids))

            async def alert(md):
                calls.append(("alert", md[:5]))
                return True

            with mock.patch.object(reports, "due_digests", return_value=[("weekly:x", "7d")]), \
                    mock.patch.object(reports, "mark_sent"):
                asyncio.run(reports.send_due(con, cfg, 1, alert, refresh))
            self.assertEqual(calls[0], ("refresh", [5]))                  # the pulse story, right before it
            self.assertEqual(calls[1][0], "alert")
            self.assertEqual(calls[2], ("refresh", None))                 # the active stories before a digest
            self.assertEqual(calls[3][0], "alert")
            self.assertIsNotNone(con.execute("SELECT pulse_sent_at FROM stories").fetchone()[0])

    def test_a_pass_from_the_command_line_feeds_the_rules(self):
        with TempHome() as home:
            cfg, con = cfg_with(home), db.connect()
            db.set_meta(con, "owner_id", 1)
            add_rule(con)
            t = db.now() - 600
            api = FakeApi({7: (story(7, t), [view(10, t + 60)])}, [user(10, "Ann", None, "ann", contact=True)])
            n, found = asyncio.run(collector.sync(api, con, cfg, 1))
            self.assertEqual((n, found), (1, 1))
            self.assertEqual(con.execute("SELECT user_id, status FROM deliveries").fetchall()[0][:], (10, "queued"))
            self.assertFalse(pace.stale(con))

    def test_tables_refresh_old_data_first_and_survive_a_failure(self):
        with TempHome() as home:
            cfg, con = cfg_with(home), db.connect()
            ran = []

            async def run(c, **todo):
                ran.append(todo)
                db.set_state(con, "last_poll_at", db.now())

            self.assertTrue(cli.freshen(cfg, run))           # never polled: old
            self.assertFalse(cli.freshen(cfg, run))          # just polled: fresh
            self.assertEqual(ran, [{"general": True, "story": None, "channel": None}])
            db.set_state(con, "last_poll_at", db.now() - 3600)

            async def broken(c, **todo):
                raise ConnectionError("offline")

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertFalse(cli.freshen(cfg, broken))
            self.assertIn("could not refresh from Telegram (ConnectionError)", err.getvalue())

    def test_no_sync_flag(self):
        args = cli.build_parser().parse_args(["table", "story", "last", "--no-sync"])
        self.assertTrue(args.no_sync)
        self.assertTrue(cli.build_parser().parse_args(["dashboard", "--no-sync"]).no_sync)


if __name__ == "__main__":
    unittest.main()
