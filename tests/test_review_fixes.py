"""Regression tests for the independent review of 1.0.0 (Codex CLI, 2026-10-06). Each test names the finding."""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from _helpers import FakeApi, TempHome, make, no_sleep, story, user, view

from tgstories import analytics, collector, config, db, notify, rules, sender, tables

T0 = 1_760_000_000


def write_cfg(home, **auto):
    cfg = {"timezone": "UTC", "autoresponder": {"enabled": True, "quiet_hours": [], "delay_s": [30, 30],
                                                 "skip_if_owner_wrote_hours": 0, **auto}}
    (home / "telegram-stories.json").write_text(json.dumps(cfg), encoding="utf-8")
    return config.load_config()


def base_db():
    con = db.connect()
    db.set_meta(con, "owner_id", 1)
    for u in (user(10, "Ann", None, "ann", contact=True, mutual=True), user(11, "Bob", None, "bob", contact=True)):
        db.upsert_person(con, collector.tg.user_row(u))
    db.upsert_story(con, collector.tg.story_row(story(100, T0), 1))
    return con


def add_rule(con, cfg, status="shadow", **over):
    spec = {"name": "t", "stories": {"mode": "ids", "ids": [100]}, "audience": {"mode": "all"},
            "action": {"type": "dm", "text": "Hi"}}
    spec.update(over)
    n = rules.normalize(spec, cfg)
    cur = con.execute("INSERT INTO rules(name,status,spec,digest,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                      ("t", status, db.dumps(n), rules.digest(n), T0, T0))
    return cur.lastrowid, rules.digest(n)


def rule_args(action, rid, **kw):
    return SimpleNamespace(action=action, id=rid, json=kw.get("json"), file=None, confirm=kw.get("confirm"))


class FakeClient:
    def __init__(self, on_lookup=None):
        self.sent, self.get_kwargs, self.on_lookup = [], [], on_lookup

    async def send_message(self, peer, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(id=len(self.sent))

    async def get_messages(self, peer, **kw):
        self.get_kwargs.append(kw)
        return []


class FakeSendApi:
    def __init__(self, client, on_lookup=None, fresh=None):
        self.client, self.on_lookup, self.fresh = client, on_lookup, fresh

    async def input_user(self, user_id, access_hash):
        if self.on_lookup:
            self.on_lookup()        # something happens while the sender awaits the network
        return SimpleNamespace(user_id=user_id)

    async def fresh_user(self, peer):
        return self.fresh


def queue(con, rid, uid=10, digest=None):
    con.execute("INSERT INTO deliveries(rule_id,user_id,peer_id,story_id,status,due_at,created_at,rule_digest) "
                "VALUES(?,?,1,100,'queued',0,?,?)", (rid, uid, db.now(), digest))


class SendGuards(unittest.TestCase):
    def test_1_activation_is_atomic_with_its_digest(self):
        with TempHome() as home:
            cfg = write_cfg(home)
            con = base_db()
            rid, d_old = add_rule(con, cfg)
            # someone updates the rule after the owner saw the preview with d_old
            rules.cli(cfg, rule_args("update", rid, json=json.dumps(
                {"name": "B", "stories": {"mode": "all"}, "action": {"type": "dm", "text": "Other"}})))
            with self.assertRaises(SystemExit):
                rules.cli(cfg, rule_args("activate", rid, confirm=d_old))
            self.assertEqual(con.execute("SELECT status FROM rules WHERE id=?", (rid,)).fetchone()[0], "shadow")

    def test_2_stop_while_sender_awaits_network_sends_nothing(self):
        with TempHome() as home:
            cfg = write_cfg(home)
            con = base_db()
            rid, dg = add_rule(con, cfg, status="active")
            queue(con, rid, digest=dg)
            client = FakeClient()
            api = FakeSendApi(client, on_lookup=lambda: db.set_state(con, "kill_switch", 1))
            asyncio.run(sender.Sender(api, con, cfg).run_once())
            self.assertEqual(client.sent, [])
            self.assertNotEqual(con.execute("SELECT status FROM deliveries").fetchone()[0], "sent")

    def test_2b_rule_to_shadow_while_awaiting_sends_nothing(self):
        with TempHome() as home:
            cfg = write_cfg(home)
            con = base_db()
            rid, dg = add_rule(con, cfg, status="active")
            queue(con, rid, digest=dg)
            client = FakeClient()
            api = FakeSendApi(client, on_lookup=lambda: con.execute("UPDATE rules SET status='shadow'"))
            asyncio.run(sender.Sender(api, con, cfg).run_once())
            self.assertEqual(client.sent, [])

    def test_3_old_queue_never_sends_after_a_rule_change(self):
        with TempHome() as home:
            cfg = write_cfg(home)
            con = base_db()
            rid, dg = add_rule(con, cfg, status="active")
            queue(con, rid, digest=dg)
            rules.cli(cfg, rule_args("update", rid, json=json.dumps(
                {"name": "B", "stories": {"mode": "all"}, "action": {"type": "dm", "text": "New text"}})))
            row = con.execute("SELECT status, reason FROM deliveries").fetchone()
            self.assertEqual((row[0], row[1]), ("skipped", "rule changed"))

    def test_4_config_switch_off_reaches_a_running_sender(self):
        with TempHome() as home:
            cfg = write_cfg(home)
            con = base_db()
            rid, dg = add_rule(con, cfg, status="active")
            queue(con, rid, digest=dg)
            s = sender.Sender(FakeSendApi(FakeClient()), con, cfg)   # started while enabled
            write_cfg(home, enabled=False)                            # owner switches it off in the file
            asyncio.run(s.run_once())
            self.assertEqual(con.execute("SELECT reason FROM deliveries").fetchone()[0], "autoresponder_disabled")

    def test_5_never_message_matches_secondary_usernames(self):
        with TempHome() as home:
            con = base_db()
            u = user(12, "Cid", None, "primary", contact=True)
            u.usernames = [make("Username", username="primary", active=True),
                           make("Username", username="Secondary", active=True)]
            db.upsert_person(con, collector.tg.user_row(u))
            cfg = write_cfg(home, never_message=["@secondary"])
            spec = rules.normalize({"stories": {"mode": "all"}, "action": {"type": "dm", "text": "x"}}, cfg)
            self.assertEqual(rules.precheck(con, cfg, spec, 12), "never_message")

    def test_6_uncertain_send_is_never_retried(self):
        with TempHome() as home:
            cfg = write_cfg(home)
            con = base_db()
            rid, dg = add_rule(con, cfg, status="active")
            queue(con, rid, digest=dg)

            class Flaky(FakeClient):
                async def send_message(self, peer, text, **kw):
                    raise TimeoutError("response lost")

            asyncio.run(sender.Sender(FakeSendApi(Flaky()), con, cfg).run_once())
            row = con.execute("SELECT status, reason FROM deliveries").fetchone()
            self.assertEqual(row[0], "failed")
            self.assertIn("uncertain", row[1])
            # a crash between send and the "sent" write leaves `sending`, recovered as failed — no resend
            con.execute("UPDATE deliveries SET status='sending'")
            self.assertEqual(sender.Sender(None, con, cfg).recover_interrupted(), 1)

    def test_7_next_binds_only_after_activation(self):
        with TempHome() as home:
            cfg = write_cfg(home)
            con = base_db()
            rid, dg = add_rule(con, cfg, stories={"mode": "next"})
            eng = rules.Engine(con, cfg)
            eng.on_event("story", {"peer_id": 1, "story_id": 200, "posted_at": db.now()})
            self.assertIsNone(con.execute("SELECT bound_story_id FROM rules").fetchone()[0])

    def test_9_incomplete_profile_keeps_paid_stars(self):
        with TempHome():
            con = base_db()
            db.upsert_person(con, collector.tg.user_row(user(13, "P", None, "p", paid=50)))
            minimal = make("User", id=13, first_name="P", last_name=None, username="p", usernames=None, min=True)
            db.upsert_person(con, collector.tg.user_row(minimal))
            self.assertEqual(con.execute("SELECT paid_stars FROM people WHERE user_id=13").fetchone()[0], 50)

    def test_9b_fresh_profile_before_sending_catches_new_paywall(self):
        with TempHome() as home:
            cfg = write_cfg(home)
            con = base_db()
            rid, dg = add_rule(con, cfg, status="active")
            queue(con, rid, digest=dg)
            fresh = collector.tg.user_row(user(10, "Ann", None, "ann", contact=True, mutual=True, paid=100))
            client = FakeClient()
            asyncio.run(sender.Sender(FakeSendApi(client, fresh=fresh), con, cfg).run_once())
            self.assertEqual(client.sent, [])
            self.assertEqual(con.execute("SELECT reason FROM deliveries").fetchone()[0], "paid_messages")

    def test_10_caps_count_the_scope_recorded_at_sending(self):
        with TempHome() as home:
            cfg = write_cfg(home, caps={"contacts": 1, "dialog": 40, "all": 10, "per_hour": 15})
            con = base_db()
            rid, dg = add_rule(con, cfg, status="active")
            con.execute("INSERT INTO deliveries(rule_id,user_id,status,sent_at,scope,created_at) "
                        "VALUES(?,11,'sent',?,'contacts',?)", (rid, db.now(), db.now()))
            con.execute("UPDATE rules SET spec=json_set(spec,'$.scope','all')")   # spec edited afterwards
            spec = rules.normalize({"stories": {"mode": "all"}, "action": {"type": "dm", "text": "x"}}, cfg)
            self.assertEqual(sender.caps_ok(con, cfg, spec, rid), "cap_contacts_day")

    def test_11_owner_check_looks_for_the_owners_own_message(self):
        with TempHome() as home:
            cfg = write_cfg(home, skip_if_owner_wrote_hours=24)
            con = base_db()
            client = FakeClient()
            s = sender.Sender(FakeSendApi(client), con, cfg)
            asyncio.run(s._owner_wrote_recently(SimpleNamespace()))
            self.assertEqual(client.get_kwargs[-1].get("from_user"), "me")


class CollectionAndMetrics(unittest.TestCase):
    def test_8_growth_absorbed_by_a_story_refresh_is_still_read(self):
        with TempHome():
            con = base_db()
            st = {100: (story(100, T0), [view(10, T0 + 60)])}
            api = FakeApi(st, [user(10, "Ann", None, "ann"), user(11, "Bob", None, "bob")])
            col = collector.Collector(api, con, config.load_config(), 1, sleep=no_sleep)
            asyncio.run(col.poll_counters([100]))                      # list read once
            st[100][1].insert(0, view(11, T0 + 120))                   # a new viewer arrives
            st[100] = (story(100, T0, views=2), st[100][1])            # the 5-minute refresh sees views=2 first
            asyncio.run(col.refresh_active())
            api.calls.clear()
            asyncio.run(col.poll_counters([100]))
            self.assertTrue([c for c in api.calls if c[0] == "views_list"])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM views").fetchone()[0], 2)

    def test_16_last_activity_uses_the_latest_view(self):
        with TempHome():
            con = base_db()
            con.execute("UPDATE stories SET list_available=1, list_synced=?", (T0,))
            db.upsert_view(con, {"peer_id": 1, "story_id": 100, "user_id": 10, "viewed_at": T0})
            db.upsert_view(con, {"peer_id": 1, "story_id": 100, "user_id": 10, "viewed_at": T0 + 90 * 86400})
            p = {x["user_id"]: x for x in analytics.people(con, 1, now=T0 + 90 * 86400 + 60)}[10]
            self.assertEqual(p["last"], T0 + 90 * 86400)
            self.assertNotEqual(p["status"], "lost")

    def test_17_unknown_list_is_not_zero(self):
        with TempHome():
            con = base_db()   # story 100 was never list-synced
            self.assertIsNone(analytics.story_at(con, 1, 100, 1))

    def test_21_table_respects_the_message_size(self):
        with TempHome():
            cfg = config.load_config()
            tb = tables.Table(cols=[tables.Col("a", "A")])
            tb.rows = [{"a": "x" * 400} for _ in range(140)]
            tb.plain = [{"a": "x" * 400} for _ in range(140)]
            out = tables._emit([tb], "md", cfg, "t")
            self.assertLessEqual(len(out), tables.MESSAGE_BUDGET + 400)
            self.assertIn("CSV:", out)

    def test_24_plain_fallback_keeps_escaped_pipes(self):
        plain = notify.to_plain("| a | b |\n|---|---|\n| A\\|B | 2 |")
        self.assertIn("A|B · 2", plain)


if __name__ == "__main__":
    unittest.main()
