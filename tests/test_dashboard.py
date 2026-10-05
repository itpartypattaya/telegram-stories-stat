"""The HTML dashboard: same numbers as the tables, hostile text stays text, nothing loads from the network."""
from __future__ import annotations

import asyncio
import json
import os
import re
import unittest
from types import SimpleNamespace

from _helpers import FakeApi, TempHome, no_sleep, story, user, view

from tgstories import analytics, collector, config, dashboard, db

T0 = 1_760_000_000
EVIL_NAME = "<script>alert(1)</script>"
EVIL_CAPTION = "</script><img src=x onerror=alert(2)> {{DATA}} & more"


def history(n_stories=6, base_views=10):
    users = [user(100 + i, f"U{i}", None, f"u{i}", contact=True) for i in range(40)]
    users.append(user(200, EVIL_NAME, None, None))
    st = {}
    for k in range(n_stories):
        t = T0 + k * 86400
        vs = [view(100 + i, t + 600 + i * 30, reaction="🔥" if i == 0 else None) for i in range(base_views + k)]
        vs.append(view(200, t + 7200))
        st[1000 + k] = (story(1000 + k, t, caption=EVIL_CAPTION if k == 5 else f"s{k}"), list(reversed(vs)))
    return st, users


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.home = TempHome()
        self.path = self.home.__enter__()
        self.con = db.connect()
        db.set_meta(self.con, "owner_id", 1)
        self.con.execute("INSERT INTO peers(peer_id,kind,title,username,added_at) VALUES(1,'self','Owner','owner',0)")
        st, users = history()
        self.cfg = config._merge(config.load_config(), {"timezone": "UTC"})
        col = collector.Collector(FakeApi(st, users), self.con, self.cfg, 1, sleep=no_sleep)
        asyncio.run(col.backfill(thumbs=False, progress=None))
        self.con.execute("UPDATE stories SET views = viewers_listed + 1")   # Telegram's counter, incognito included

    def tearDown(self):
        self.con.close()
        self.home.__exit__(None, None, None)

    def page(self, cfg=None, **kw):
        cfg = cfg or self.cfg
        return dashboard.render(self.con, cfg, dashboard.collect(self.con, cfg), **kw)

    def kpi(self, page, label):
        m = re.search(r'<div class="l">' + re.escape(label) + r'</div><div class="v">([^<]*)</div>', page)
        self.assertIsNotNone(m, label)
        return m.group(1)

    def test_numbers_match_the_tables(self):
        page = self.page()
        sm = analytics.summary(self.con, 1, 0, db.now())
        # "all time" is the last panel; its cards come after the "panel-all" anchor
        tail = page[page.index('id="panel-all"'):]
        self.assertEqual(self.kpi(tail, "Stories"), str(sm["count"]))
        self.assertEqual(self.kpi(tail, "Views"), f"{sm['views_total']:,}")
        self.assertEqual(self.kpi(tail, "Unique viewers"), str(sm["unique_viewers"]))
        self.assertIn(f'<span class="chip">Viewers <b>{len(analytics.people(self.con, 1))}</b>', page)
        # the default tab is the shortest period with 10+ stories, else "all time"
        self.assertIn('id="p-all" checked', page)

    def test_hostile_names_and_captions_stay_text(self):
        page = self.page()
        self.assertNotIn(EVIL_NAME, page)
        self.assertNotIn("<img src=x", page)
        self.assertEqual(len(re.findall(r"<script", page)), 2)          # the data block and the page script
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)    # escaped in the people table
        blob = re.search(r'<script type="application/json" id="dash-data">(.*?)</script>', page, re.S).group(1)
        data = json.loads(blob)
        self.assertIn(EVIL_NAME, [p[0] for p in data["people"]])          # intact once parsed
        self.assertIn(EVIL_CAPTION, [s["c"] for s in data["stories"].values()])
        self.assertNotIn("{{", page.replace("{{DATA}}", ""))              # no placeholder left unfilled
        bare = dashboard.TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("page template, without data", bare)               # opened as is, it says what it is
        self.assertNotIn("page template, without data", page)
        self.assertIn("<title>Stories dashboard · Owner</title>", page)

    def test_no_external_resources(self):
        page = self.page()
        self.assertIn("default-src 'none'", page)
        for url in re.findall(r'(?:src|href)="([^"]+)"', page):
            self.assertTrue(url.startswith("https://t.me/"), url)
        self.assertNotRegex(page, r"@import|url\((?!\"data:)")

    def test_viewer_lists_are_complete(self):
        data = json.loads(re.search(r'id="dash-data">(.*?)</script>', self.page(), re.S).group(1))
        stories = {str(r[0]) for r in self.con.execute("SELECT DISTINCT story_id FROM views")}
        self.assertEqual(set(data["views"]), stories)
        for sid, rows in data["views"].items():
            listed = self.con.execute("SELECT COUNT(*) FROM views WHERE story_id=?", (int(sid),)).fetchone()[0]
            self.assertEqual(len(rows), listed)
        fire = data["reacts"].index("🔥") + 1
        self.assertTrue(all(any(len(r) == 3 and r[2] == fire for r in rows) for rows in data["views"].values()))

    def test_russian_page_and_private_file(self):
        cfg = config._merge(self.cfg, {"locale": "ru"})
        path = dashboard.build(cfg, con=self.con)
        page = path.read_text(encoding="utf-8")
        self.assertIn('<html lang="ru">', page)
        self.assertIn("Дашборд сторис", page)
        self.assertIn("Перестали смотреть", page)
        self.assertEqual(path, config.data_dir() / "dashboard" / "dashboard.html")
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_thumbnails_only_jpeg_and_only_for_the_best(self):
        folder = config.data_dir() / "thumbs"
        folder.mkdir(parents=True, exist_ok=True)
        for sid in range(1000, 1006):
            raw = b"\x89PNG\r\n" if sid == 1003 else b"\xff\xd8\xff\xe0" + bytes(64)
            (folder / f"1_{sid}.jpg").write_bytes(raw)
            self.con.execute("UPDATE stories SET thumb=? WHERE story_id=?", (f"1_{sid}.jpg", sid))
        page = self.page()
        embedded = set(re.findall(r"\.th-(\d+)\{background-image", page))
        ranked = [i for i in analytics.stories_in(self.con, 1, 0, db.now()) if i["index"] is not None]
        self.assertTrue(embedded)
        self.assertNotIn("1003", embedded)                                  # not a JPEG
        self.assertNotIn("1000", embedded)                                  # first story: no usual to beat
        self.assertLessEqual(embedded, {str(i["story_id"]) for i in ranked})
        self.assertNotIn("background-image", self.page(thumbs=False).split("</style>")[0][-2000:])

    def test_empty_period_and_no_rules(self):
        page = self.page()
        self.assertIn("No stories in this period.", page[page.index('id="panel-30d"'):page.index('id="panel-90d"')])
        self.assertIn("No rules.", page)
        self.assertIn("Autoresponder is off", page)

    def test_monthly_and_speed(self):
        months = analytics.monthly(self.con, 1, config.get_tz(self.cfg))
        viewers = self.con.execute("SELECT COUNT(DISTINCT user_id) FROM views").fetchone()[0]
        self.assertEqual(sum(m["new"] for m in months), viewers)
        self.assertEqual(sum(m["stories"] for m in months), 6)
        sp = analytics.speed(self.con, 1, 0, db.now())
        shares = [sp["share"][h] for h in (1, 6, 24, 48)]
        self.assertEqual(shares, sorted(shares))
        self.assertEqual(shares[-1], 1.0)
        self.assertEqual(sp["stories"], 6)


class LiveThumbnails(unittest.TestCase):
    def test_new_stories_get_a_thumbnail_once(self):
        with TempHome():
            con = db.connect()
            st = {1000 + k: (story(1000 + k, db.now() - 600 + k), []) for k in range(6)}
            api = FakeApi(st, [])
            calls = []

            class Client:
                async def download_media(self, media, file=None, thumb=None):
                    calls.append(file)
                    with open(file, "wb") as fh:
                        fh.write(b"\xff\xd8")

            api.client = Client()
            col = collector.Collector(api, con, config.load_config(), 1, sleep=no_sleep)
            asyncio.run(col.refresh_active())
            self.assertEqual(len(calls), 5)                 # at most five per pass
            asyncio.run(col.refresh_active())
            self.assertEqual(len(calls), 6)                 # the rest on the next pass
            asyncio.run(col.refresh_active())
            self.assertEqual(len(calls), 6)                 # never twice
            self.assertEqual(con.execute("SELECT COUNT(*) FROM stories WHERE thumb IS NOT NULL").fetchone()[0], 6)

    def test_a_failed_download_is_not_retried(self):
        with TempHome():
            con = db.connect()
            api = FakeApi({1000: (story(1000, db.now() - 60), [])}, [])
            calls = []

            class Client:
                async def download_media(self, media, file=None, thumb=None):
                    calls.append(file)
                    raise ConnectionError("flaky")

            api.client = Client()
            col = collector.Collector(api, con, config.load_config(), 1, sleep=no_sleep)
            for _ in range(3):
                asyncio.run(col.refresh_active())
            self.assertEqual(len(calls), 1)

    def test_thumbnails_off_in_the_config(self):
        with TempHome():
            con = db.connect()
            api = FakeApi({1000: (story(1000, db.now() - 60), [])}, [])
            api.client = SimpleNamespace(download_media=None)   # would raise if called
            cfg = config._merge(config.load_config(), {"backfill": {"thumbs": False}})
            asyncio.run(collector.Collector(api, con, cfg, 1, sleep=no_sleep).refresh_active())
            self.assertIsNone(con.execute("SELECT thumb FROM stories").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
