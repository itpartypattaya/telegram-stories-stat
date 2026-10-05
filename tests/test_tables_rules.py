"""Tables (Markdown for Telegram Rich Messages, CSV), rules engine, sender guards — synthetic data only."""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from _helpers import FakeApi, TempHome, no_sleep, story, user, view

from tgstories import collector, config, db, notify, rules, sender, tables

T0 = 1_760_000_000


def loaded(cfg_over=None):
    con = db.connect()
    db.set_meta(con, "owner_id", 1)
    users = [user(10, "Ann", "A|nn", "ann_x", contact=True, mutual=True, close=True),
             user(11, "Bob", None, None, contact=True), user(12, "Cid", None, "cid"),
             user(13, "Paid", None, "paid", contact=True, paid=50), user(14, "Bot", None, "botty", bot=True)]
    s1 = story(100, T0, caption="Hello | world")
    st = {100: (s1, [view(14, T0 + 50), view(13, T0 + 40), view(12, T0 + 7200), view(11, T0 + 3000, "❤"),
                     view(10, T0 + 600)])}
    cfg = config.load_config()
    if cfg_over:
        cfg = config._merge(cfg, cfg_over)
    col = collector.Collector(FakeApi(st, users), con, cfg, 1, sleep=no_sleep)
    asyncio.run(col.backfill(thumbs=False, progress=None))
    return con, cfg


class TablesTest(unittest.TestCase):
    def test_story_table_markdown(self):
        with TempHome():
            con, cfg = loaded({"timezone": "UTC"})
            tb = tables.story_table(con, cfg, 1, "last")
            md = tb.to_md()
            lines = md.splitlines()
            header = next(i for i, l in enumerate(lines) if l.startswith("| #"))
            self.assertRegex(lines[header + 1], r"^\|---:\|")
            body = [l for l in lines[header + 2:] if l.startswith("|")]
            self.assertEqual(len(body), 5)
            # every row has the same number of cells — escaped pipes don't break the table
            widths = {l.count("|") - l.count("\\|") for l in lines[header:header + 2 + len(body)]}
            self.assertEqual(len(widths), 1)
            ann = next(l for l in body if "Ann" in l)
            self.assertIn("[@ann\\_x](https://t.me/ann_x)", ann)      # nick is a link
            self.assertIn("A\\|nn", ann)                              # pipe in a name is escaped
            self.assertIn("⭐👥", ann)
            bob = next(l for l in body if "Bob" in l)
            self.assertIn("| Bob |", bob)                             # no username → plain name (tg:// links do not work from bots)
            self.assertIn("| — |", bob)

    def test_csv_has_name_and_username(self):
        with TempHome():
            con, cfg = loaded()
            tb = tables.story_table(con, cfg, 1, "last")
            csv_text = tb.to_csv()
            head = csv_text.splitlines()[0].split(",")
            for col in ("user_id", "first_name", "last_name", "username", "viewed_at"):
                self.assertIn(col, head)
            self.assertIn("ann_x", csv_text)

    def test_summary_and_text_formats(self):
        with TempHome():
            con, cfg = loaded({"locale": "ru"})
            tb = tables.summary_table(con, cfg, 1, 0, T0 + 86400)
            md = tb.to_md()
            self.assertIn("Сводка по сторис", md)
            self.assertIn("Hello \\| world", md)
            self.assertNotIn("](", tb.to_text())
            json.loads(tb.to_json())

    def test_caption_cut_never_splits_links_or_usernames(self):
        cut = tables.caption_cut
        self.assertEqual(cut("Read https://example.com/very/long/path today", 40), "Read 🔗 today")
        self.assertEqual(cut("⚡️ Обмен тут: @it_exchange_pattaya_bot и в чате", 28), "⚡️ Обмен тут…")
        self.assertNotIn("@it_exchange", cut("⚡️ Обмен тут: @it_exchange_pattaya_bot и в чате", 28))
        self.assertEqual(cut("short", 28), "short")

    def test_plain_fallback(self):
        md = "**T**\n\n| a | b |\n|---|---:|\n| [@x](https://t.me/x) | 2 |"
        plain = notify.to_plain(md)
        self.assertNotIn("|", plain)
        self.assertIn("@x · 2", plain)


class RulesTest(unittest.TestCase):
    def spec(self, **over):
        s = {"name": "t", "stories": {"mode": "ids", "ids": [100]}, "audience": {"mode": "all"},
             "trigger": "view", "action": {"type": "dm", "text": "Hi {first_name}"}}
        s.update(over)
        return s

    def test_normalize_and_digest(self):
        cfg = config.load_config()
        n = rules.normalize(self.spec(), cfg)
        self.assertEqual(n["scope"], "contacts")              # default scope
        self.assertEqual(n["action"]["variants"], ["Hi {first_name}"])
        d1 = rules.digest(n)
        n2 = rules.normalize(self.spec(action={"type": "dm", "text": "Hi!"}), cfg)
        self.assertNotEqual(d1, rules.digest(n2))
        with self.assertRaises(SystemExit):
            rules.normalize(self.spec(scope="everyone"), cfg)

    def test_precheck_guards(self):
        with TempHome():
            con, cfg = loaded({"autoresponder": {"never_message": ["@cid"]}})
            spec = rules.normalize(self.spec(scope="all"), cfg)
            self.assertIsNone(rules.precheck(con, cfg, spec, 10))
            self.assertEqual(rules.precheck(con, cfg, spec, 12), "never_message")
            self.assertEqual(rules.precheck(con, cfg, spec, 13), "paid_messages")
            self.assertEqual(rules.precheck(con, cfg, spec, 14), "bot")
            self.assertEqual(rules.precheck(con, cfg, spec, 1), "owner")
            contacts = rules.normalize(self.spec(), cfg)
            cfg2 = config._merge(cfg, {"autoresponder": {"never_message": []}})
            self.assertEqual(rules.precheck(con, cfg2, contacts, 12), "scope_contacts")
            notify_spec = rules.normalize(self.spec(action={"type": "notify"}), cfg)
            self.assertIsNone(rules.precheck(con, cfg2, notify_spec, 12))   # notify writes to nobody

    def _rule(self, con, cfg, status, spec):
        n = rules.normalize(spec, cfg)
        cur = con.execute("INSERT INTO rules(name,status,spec,digest,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                          (n["name"], status, db.dumps(n), rules.digest(n), T0, T0))
        return cur.lastrowid

    def test_engine_shadow_and_dedupe(self):
        with TempHome():
            con, cfg = loaded()
            rid = self._rule(con, cfg, "shadow", self.spec())
            eng = rules.Engine(con, cfg)
            row = {"peer_id": 1, "story_id": 100, "user_id": 10, "viewed_at": T0 + 600}
            self.assertEqual(eng.on_event("view", row), 1)
            self.assertEqual(eng.on_event("view", row), 0)    # one message per person per rule
            d = con.execute("SELECT status FROM deliveries WHERE rule_id=?", (rid,)).fetchone()
            self.assertEqual(d[0], "shadow")

    def test_engine_queues_for_active_and_skips_strangers(self):
        with TempHome():
            con, cfg = loaded()
            rid = self._rule(con, cfg, "active", self.spec())
            eng = rules.Engine(con, cfg)
            eng.on_event("view", {"peer_id": 1, "story_id": 100, "user_id": 10, "viewed_at": T0})
            eng.on_event("view", {"peer_id": 1, "story_id": 100, "user_id": 12, "viewed_at": T0})
            got = dict(con.execute("SELECT user_id, status FROM deliveries WHERE rule_id=?", (rid,)).fetchall())
            self.assertEqual(got, {10: "queued", 12: "skipped"})

    def test_next_story_binding(self):
        with TempHome():
            con, cfg = loaded()
            rid = self._rule(con, cfg, "active", self.spec(stories={"mode": "next"}))
            eng = rules.Engine(con, cfg)
            eng.on_event("story", {"peer_id": 1, "story_id": 200, "posted_at": T0 + 10})
            self.assertEqual(con.execute("SELECT bound_story_id FROM rules WHERE id=?", (rid,)).fetchone()[0], 200)

    def test_quiet_hours(self):
        from datetime import datetime, timezone
        cfg = config._merge(config.load_config(), {"timezone": "UTC", "autoresponder": {"quiet_hours": ["22:00", "09:00"]}})
        late = int(datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc).timestamp())
        end = rules._in_quiet(cfg, late)
        self.assertEqual((end.day, end.hour), (2, 9))
        noon = int(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(rules._in_quiet(cfg, noon))


class SenderTest(unittest.TestCase):
    def test_classify(self):
        PeerFloodError = type("PeerFloodError", (Exception,), {})
        Privacy = type("UserPrivacyRestrictedError", (Exception,), {})
        Flood = type("FloodWaitError", (Exception,), {"seconds": 30})
        self.assertEqual(sender.classify(PeerFloodError())[0], "stop")
        self.assertEqual(sender.classify(Privacy()), ("skip", "privacy"))
        self.assertEqual(sender.classify(Exception("RPCError 400: ALLOW_PAYMENT_REQUIRED_500")),
                         ("skip", "paid_messages"))
        self.assertEqual(sender.classify(Flood())[0], "retry")

    def test_disabled_autoresponder_sends_nothing(self):
        with TempHome():
            con, cfg = loaded()
            n = rules.normalize({"name": "t", "stories": {"mode": "all"}, "action": {"type": "dm", "text": "x"}}, cfg)
            con.execute("INSERT INTO rules(id,name,status,spec,digest) VALUES(1,'t','active',?,?)",
                        (db.dumps(n), rules.digest(n)))
            con.execute("INSERT INTO deliveries(rule_id,user_id,peer_id,story_id,status,due_at,created_at) "
                        "VALUES(1,10,1,100,'queued',0,?)", (db.now(),))
            sent_calls = []

            class Client:
                async def send_message(self, *a, **k):
                    sent_calls.append(a)
                    return SimpleNamespace(id=1)

            api = SimpleNamespace(client=Client())
            s = sender.Sender(api, con, cfg)     # autoresponder.enabled is false by default
            self.assertEqual(asyncio.run(s.run_once()), 0)
            self.assertEqual(sent_calls, [])
            self.assertEqual(con.execute("SELECT status, reason FROM deliveries").fetchone()[1],
                             "autoresponder_disabled")


if __name__ == "__main__":
    unittest.main()
