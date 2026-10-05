"""The installer creates everything on a clean home and never overwrites on a re-run."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import unittest

from _helpers import SKILL, TempHome

EXPECTED_TABLES = {"meta", "state", "peers", "stories", "story_snapshots", "channel_story_graphs", "people",
                   "people_history", "views", "view_events", "public_interactions", "story_replies",
                   "common_chats", "segments", "segment_members", "rules", "deliveries", "notifications"}


class InstallTest(unittest.TestCase):
    def run_install(self, *args):
        env = dict(os.environ)
        return subprocess.run([sys.executable, str(SKILL / "scripts" / "install.py"), "--no-service", *args],
                              capture_output=True, text=True, env=env, timeout=120)

    def test_fresh_install_and_rerun(self):
        with TempHome() as home:
            r = self.run_install()
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            db_path = home / "data" / "telegram-stories" / "stories.db"
            self.assertTrue(db_path.exists())
            con = sqlite3.connect(db_path)
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            con.close()
            self.assertTrue(EXPECTED_TABLES <= tables, EXPECTED_TABLES - tables)
            cfg_path = home / "telegram-stories.json"
            self.assertTrue(cfg_path.exists())
            for sub in ("thumbs", "exports"):
                self.assertTrue((home / "data" / "telegram-stories" / sub).is_dir())
            # the owner edits the config; a re-run must keep it
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["locale"] = "ru"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            r2 = self.run_install()
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertEqual(json.loads(cfg_path.read_text(encoding="utf-8"))["locale"], "ru")
            self.assertIn("config kept", r2.stdout)
            self.assertIn("login", r2.stdout)  # tells what to do next without a session

    def test_doctor_offline(self):
        with TempHome():
            self.run_install()
            r = subprocess.run([sys.executable, str(SKILL / "scripts" / "stories.py"), "doctor", "--offline"],
                               capture_output=True, text=True, timeout=120)
            self.assertIn("database", r.stdout)
            self.assertIn("session", r.stdout)


if __name__ == "__main__":
    unittest.main()
