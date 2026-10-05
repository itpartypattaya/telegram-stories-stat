"""Sends queued automatic messages, with every guard re-checked right before sending."""
from __future__ import annotations

import json
import logging

from . import config, db, rules

log = logging.getLogger("telegram-stories")

STOP_ALL = {"PeerFloodError"}                     # Telegram's anti-spam limit on this account
SKIP = {
    "UserPrivacyRestrictedError": "privacy",
    "PrivacyPremiumRequiredError": "privacy_premium",
    "UserIsBlockedError": "blocked_by_user",
    "InputUserDeactivatedError": "deactivated",
    "UserDeactivatedError": "deactivated",
    "UserDeactivatedBanError": "deactivated",
    "PeerIdInvalidError": "peer_invalid",
    "UserBannedInChannelError": "account_restricted",
    "ChatWriteForbiddenError": "write_forbidden",
}


def classify(exc) -> tuple[str, str]:
    """('stop'|'skip'|'retry'|'fail', reason)."""
    name = type(exc).__name__
    text = str(exc).upper()
    if name in STOP_ALL:
        return "stop", "peer_flood"
    if "ALLOW_PAYMENT_REQUIRED" in text or "PAYMENT_REQUIRED" in text:
        return "skip", "paid_messages"
    if name in SKIP:
        return "skip", SKIP[name]
    if name == "FloodWaitError":
        return "retry", f"flood_wait_{getattr(exc, 'seconds', 0)}"
    if name in ("TimeoutError", "ConnectionError", "ServerError", "RpcCallFailError"):
        return "retry", name
    return "fail", name


def caps_ok(con, cfg, spec, rule_id) -> str | None:
    now = db.now()
    ar = cfg.get("autoresponder", {})
    caps = ar.get("caps", {})
    scope = spec["scope"]
    dm = "d.status='sent' AND d.reason IS NULL"   # notify/segment deliveries carry a reason
    day = con.execute(f"""SELECT COUNT(*) FROM deliveries d JOIN rules r ON r.id=d.rule_id
                          WHERE {dm} AND d.sent_at>? AND json_extract(r.spec,'$.scope')=?""",
                      (now - 86400, scope)).fetchone()[0]
    if day >= int(caps.get(scope, 10)):
        return f"cap_{scope}_day"
    hour = con.execute(f"SELECT COUNT(*) FROM deliveries d WHERE {dm} AND d.sent_at>?", (now - 3600,)).fetchone()[0]
    if hour >= int(caps.get("per_hour", 15)):
        return "cap_hour"
    lim = spec.get("limits") or {}
    if lim.get("per_day"):
        n = con.execute("SELECT COUNT(*) FROM deliveries WHERE rule_id=? AND status='sent' AND sent_at>?",
                        (rule_id, now - 86400)).fetchone()[0]
        if n >= int(lim["per_day"]):
            return "rule_per_day"
    if lim.get("total"):
        n = con.execute("SELECT COUNT(*) FROM deliveries WHERE rule_id=? AND status='sent'", (rule_id,)).fetchone()[0]
        if n >= int(lim["total"]):
            return "rule_total"
    return None


def render(text: str, person) -> str:
    first = (person["first_name"] if person else "") or ""
    return text.replace("{first_name}", first).replace("{name}", first)


class Sender:
    def __init__(self, api, con, cfg, *, notify=None):
        self.api = api
        self.con = con
        self.cfg = cfg
        self.notify = notify   # async callable(md)

    def halted(self) -> str | None:
        if not self.cfg.get("autoresponder", {}).get("enabled"):
            return "disabled"
        if str(db.get_state(self.con, "kill_switch", "0")) == "1":
            return "kill_switch"
        until = int(db.get_state(self.con, "breaker_until", "0") or 0)
        if until > db.now():
            return "breaker"
        return None

    async def _dialog_ok(self, user_id: int, access_hash) -> bool:
        p = self.con.execute("SELECT has_dialog, dialog_checked_at FROM people WHERE user_id=?", (user_id,)).fetchone()
        if p and p["dialog_checked_at"] and db.now() - p["dialog_checked_at"] < 86400:
            return bool(p["has_dialog"])
        peer = await self.api.input_user(user_id, access_hash)
        msgs = await self.api.client.get_messages(peer, limit=1, from_user=peer)
        ok = bool(msgs)
        self.con.execute("UPDATE people SET has_dialog=?, dialog_checked_at=? WHERE user_id=?",
                         (int(ok), db.now(), user_id))
        return ok

    async def _owner_wrote_recently(self, peer) -> bool:
        hours = float(self.cfg.get("autoresponder", {}).get("skip_if_owner_wrote_hours", 24))
        if hours <= 0:
            return False
        msgs = await self.api.client.get_messages(peer, limit=1)
        if not msgs:
            return False
        m = msgs[0]
        return bool(getattr(m, "out", False)) and m.date.timestamp() > db.now() - hours * 3600

    async def run_once(self) -> int:
        """Process due deliveries. Owner notifications and segment updates always run; direct
        messages only while the autoresponder is enabled, not stopped and not in a flood pause."""
        sent = 0
        due = self.con.execute("SELECT * FROM deliveries WHERE status='queued' AND due_at<=? ORDER BY due_at LIMIT 5",
                               (db.now(),)).fetchall()
        for d in due:
            rule = self.con.execute("SELECT * FROM rules WHERE id=?", (d["rule_id"],)).fetchone()
            if rule is None or rule["status"] != "active":
                self._mark(d, "skipped", "rule_not_active")
                continue
            spec = json.loads(rule["spec"])
            reason = rules.precheck(self.con, self.cfg, spec, d["user_id"])
            if reason:
                self._mark(d, "skipped", reason)
                continue
            person = self.con.execute("SELECT * FROM people WHERE user_id=?", (d["user_id"],)).fetchone()
            action = spec["action"]
            if action["type"] == "notify":
                await self._notify_owner(d, person, rule)
                self._mark(d, "sent", "notified_owner")
                continue
            if action["type"] == "segment":
                self.con.execute("INSERT OR IGNORE INTO segment_members(segment,user_id,added_at) VALUES(?,?,?)",
                                 (action["segment"], d["user_id"], db.now()))
                self._mark(d, "sent", "segment")
                continue
            halted = self.halted()
            if halted == "breaker":
                continue
            if halted:
                self._mark(d, "skipped", "stopped" if halted == "kill_switch" else "autoresponder_disabled")
                continue
            quiet = rules._in_quiet(self.cfg, db.now())
            if quiet is not None:
                self.con.execute("UPDATE deliveries SET due_at=? WHERE rule_id=? AND user_id=?",
                                 (int(quiet.timestamp()), d["rule_id"], d["user_id"]))
                continue
            cap = caps_ok(self.con, self.cfg, spec, rule["id"])
            if cap:
                # caps reset with time: try again in an hour, give up after a day
                if db.now() - (d["created_at"] or 0) > 86400:
                    self._mark(d, "skipped", cap)
                else:
                    self.con.execute("UPDATE deliveries SET due_at=? WHERE rule_id=? AND user_id=?",
                                     (db.now() + 3600, d["rule_id"], d["user_id"]))
                continue
            try:
                peer = await self.api.input_user(d["user_id"], person["access_hash"] if person else None)
                if spec["scope"] == "dialog" and not await self._dialog_ok(d["user_id"], person["access_hash"]):
                    self._mark(d, "skipped", "scope_dialog")
                    continue
                if await self._owner_wrote_recently(peer):
                    self._mark(d, "skipped", "owner_wrote_recently")
                    continue
                variants = action["variants"]
                text = render(variants[(d["variant"] or 0) % len(variants)], person)
                msg = await self.api.client.send_message(peer, text, link_preview=False)
                self.con.execute("UPDATE deliveries SET status='sent', sent_at=?, msg_id=?, attempts=attempts+1 "
                                 "WHERE rule_id=? AND user_id=?",
                                 (db.now(), getattr(msg, "id", None), d["rule_id"], d["user_id"]))
                sent += 1
                log.info("auto-message sent: rule %s → user %s", d["rule_id"], d["user_id"])
            except Exception as exc:  # noqa: BLE001 — classified below
                verdict, reason = classify(exc)
                if verdict == "stop":
                    db.set_state(self.con, "kill_switch", 1)
                    self._mark(d, "failed", reason)
                    await self._alert(f"⛔ Autoresponder stopped: Telegram reported {reason} (anti-spam limit on "
                                      "the account). Nothing more will be sent until `stories.py start`.")
                    break
                if verdict == "retry":
                    wait = int(getattr(exc, "seconds", 60) or 60)
                    if wait > 300:
                        db.set_state(self.con, "breaker_until", db.now() + wait)
                        await self._alert(f"⏸ Autoresponder paused for {wait // 60} min: {reason}.")
                    self.con.execute("UPDATE deliveries SET due_at=?, attempts=attempts+1 WHERE rule_id=? AND "
                                     "user_id=?", (db.now() + max(wait, 60), d["rule_id"], d["user_id"]))
                    break
                if verdict == "skip":
                    self._mark(d, "skipped", reason)
                    continue
                self._mark(d, "failed", reason)
                fails = self.con.execute("SELECT COUNT(*) FROM (SELECT status FROM deliveries WHERE rule_id=? "
                                         "AND status IN ('sent','failed') ORDER BY COALESCE(sent_at, created_at) DESC "
                                         "LIMIT 3) WHERE status='failed'", (d["rule_id"],)).fetchone()[0]
                if fails >= 3:
                    self._pause(rule["id"], f"3 failures in a row ({reason})")
        return sent

    def _mark(self, d, status, reason) -> None:
        self.con.execute("UPDATE deliveries SET status=?, reason=?, attempts=attempts+1 WHERE rule_id=? AND user_id=?",
                         (status, reason, d["rule_id"], d["user_id"]))

    def _pause(self, rule_id: int, why: str) -> None:
        self.con.execute("UPDATE rules SET status='paused', paused_reason=? WHERE id=?", (why, rule_id))
        self.con.execute("UPDATE deliveries SET status='skipped', reason='rule paused' WHERE rule_id=? AND "
                         "status='queued'", (rule_id,))
        log.warning("rule %s paused: %s", rule_id, why)

    async def _alert(self, text: str) -> None:
        if self.notify:
            try:
                await self.notify(text)
            except Exception:  # noqa: BLE001
                log.exception("alert failed")

    async def _notify_owner(self, d, person, rule) -> None:
        from .tables import name_cell, nick_cell
        p = dict(person) if person else {"user_id": d["user_id"]}
        spec = json.loads(rule["spec"])
        await self._alert(f"👀 {name_cell(p, self.cfg)} {nick_cell(p, self.cfg) if p.get('username') else ''} — "
                          f"story {d['story_id']} · rule «{spec['name']}»")

    def on_hidden(self, user_id: int) -> None:
        """A viewer hid our stories or blocked us after an automatic message → pause that rule."""
        rows = self.con.execute("SELECT DISTINCT rule_id FROM deliveries WHERE user_id=? AND status='sent' AND "
                                "sent_at>?", (user_id, db.now() - 14 * 86400)).fetchall()
        for (rule_id,) in rows:
            self._pause(rule_id, f"a recipient hid your stories or blocked you (user {user_id})")

    def on_reply(self, user_id: int) -> None:
        self.con.execute("UPDATE deliveries SET replied_at=? WHERE user_id=? AND status='sent' AND replied_at IS NULL "
                         "AND sent_at>?", (db.now(), user_id, db.now() - 30 * 86400))
