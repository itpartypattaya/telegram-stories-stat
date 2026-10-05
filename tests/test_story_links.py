"""Story links: stored in the database, rebuilt on a username change, shown as "Open the story" buttons."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import unittest

from _helpers import FakeApi, TempHome, no_sleep, story, user, view

from tgstories import collector, config, dashboard, db, tables

T0 = 1_760_000_000


class Links(unittest.TestCase):
    def test_an_existing_database_gets_the_links_on_upgrade(self):
        with TempHome():
            path = config.db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            old = sqlite3.connect(str(path))
            old.executescript(db.SCHEMA_V1 + db.SCHEMA_V2 + "PRAGMA user_version=2;")
            old.executescript("""
                INSERT INTO peers(peer_id,kind,title,username) VALUES (1,'self','Me','owner'), (-5,'channel','Ch',NULL);
                INSERT INTO stories(peer_id,story_id,posted_at) VALUES (1,7,0), (1,8,0), (-5,3,0);""")
            old.close()
            con = db.connect(path)
            links = dict(con.execute("SELECT peer_id || ':' || story_id, link FROM stories").fetchall())
            self.assertEqual(links, {"1:7": "https://t.me/owner/s/7", "1:8": "https://t.me/owner/s/8", "-5:3": None})
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)

    def test_new_stories_get_a_link_and_a_new_username_rebuilds_them(self):
        with TempHome():
            con = db.connect()
            db.upsert_peer(con, 1, "self", "Me", "owner")
            db.upsert_story(con, collector.tg.story_row(story(9, T0), 1))
            self.assertEqual(con.execute("SELECT link FROM stories").fetchone()[0], "https://t.me/owner/s/9")
            db.upsert_peer(con, 1, "self", "Me", "new_name")
            self.assertEqual(con.execute("SELECT link FROM stories").fetchone()[0], "https://t.me/new_name/s/9")
            db.upsert_peer(con, 1, "self", "Me", None)                   # no username: no public link
            self.assertIsNone(con.execute("SELECT link FROM stories").fetchone()[0])

    def test_collectible_username_counts(self):
        u = user(1, "Me", None, None)
        u.usernames = [type("Username", (), {"username": "nft_name", "active": True})()]
        self.assertEqual(collector.tg.username_of(u), "nft_name")


class Buttons(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.home.__enter__()
        self.con = db.connect()
        db.set_meta(self.con, "owner_id", 1)
        db.upsert_peer(self.con, 1, "self", "Owner", "owner")
        users = [user(100 + i, f"U{i}", None, f"u{i}", contact=True) for i in range(30)]
        st = {}
        for k in range(6):
            t = T0 + k * 86400
            st[1000 + k] = (story(1000 + k, t, caption=f"s{k}"),
                            list(reversed([view(100 + i, t + 600 + i * 30) for i in range(10 + 3 * k)])))
        self.cfg = config._merge(config.load_config(), {"timezone": "UTC"})
        asyncio.run(collector.Collector(FakeApi(st, users), self.con, self.cfg, 1, sleep=no_sleep)
                    .backfill(thumbs=False, progress=None))
        self.con.execute("UPDATE stories SET views = viewers_listed")

    def tearDown(self):
        self.con.close()
        self.home.__exit__(None, None, None)

    def page(self):
        return dashboard.render(self.con, self.cfg, dashboard.collect(self.con, self.cfg))

    def test_cards_and_lists_carry_the_link(self):
        page = self.page()
        gallery = page[page.index('class="gallery"'):]
        gallery = gallery[:gallery.index("</div></div>")]
        self.assertRegex(gallery, r'<a class="go" href="https://t\.me/owner/s/10\d\d"[^>]*>Open the story ↗</a>')
        self.assertIn('href="https://t.me/owner/s/1005"', page)            # the ID in the stories table
        data = json.loads(re.search(r'id="dash-data">(.*?)</script>', page, re.S).group(1))
        self.assertEqual(data["stories"]["1003"]["u"], "https://t.me/owner/s/1003")

    def test_a_tampered_link_never_reaches_the_page(self):
        self.con.execute("UPDATE stories SET link='javascript:alert(1)' WHERE story_id=1005")
        self.con.execute("UPDATE stories SET link='https://evil.example/s/1' WHERE story_id=1004")
        page = self.page()
        self.assertNotIn("javascript:", page)
        self.assertNotIn("evil.example", page)

    def test_no_username_no_button(self):
        db.upsert_peer(self.con, 1, "self", "Owner", None)
        page = self.page()
        self.assertNotIn('class="go"', page.split("</style>", 1)[1])

    def test_tables_and_csv_carry_the_link(self):
        tb = tables.story_table(self.con, self.cfg, 1, "1004")
        self.assertIn("[Open the story](https://t.me/owner/s/1004)", tb.title)
        csv = tables.summary_table(self.con, self.cfg, 1, 0, db.now()).to_csv()
        self.assertIn("link", csv.splitlines()[0].split(","))
        self.assertIn("https://t.me/owner/s/1000", csv)


if __name__ == "__main__":
    unittest.main()
