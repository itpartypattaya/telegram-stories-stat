"""Autoresponder rules and audience segments.

A rule = which stories × which audience × trigger × action × limits.
Life cycle: draft → shadow (records who WOULD get the message, sends nothing)
→ active (only via `rule activate <id> --confirm <digest>`, where the digest
is printed by `rule preview` and covers everything that decides who gets what)
→ paused / done. Any change to the spec puts the rule back to shadow.

Guards that no rule can switch off: autoresponder.enabled in the config, the
kill switch, scope (contacts by default), never_message, bots and deleted
accounts, people who charge Stars for messages, cooldown between automatic
messages, daily and hourly caps. Details: references/rules.md.
"""
from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timedelta

from . import config, db

SCOPES = ("contacts", "dialog", "all")
TRIGGERS = ("view", "reaction", "reply")
STORY_MODES = ("ids", "next", "tag", "all")
AUDIENCE_MODES = ("all", "users", "segment", "reacted", "new", "status")
ACTIONS = ("dm", "notify", "segment")

TEMPLATE = {
    "name": "Thank everyone who views the next story",
    "stories": {"mode": "next"},
    "audience": {"mode": "all"},
    "scope": "contacts",
    "trigger": "view",
    "action": {"type": "dm", "text": "Hi {first_name}! Thanks for watching 🙌"},
    "limits": {"per_day": 20, "total": 100},
}


# ── spec ───────────────────────────────────────────────────────────────────

def normalize(spec: dict, cfg: dict) -> dict:
    if not isinstance(spec, dict):
        raise SystemExit("rule spec must be a JSON object")
    out = {
        "name": str(spec.get("name") or "rule").strip()[:80],
        "stories": dict(spec.get("stories") or {"mode": "next"}),
        "audience": dict(spec.get("audience") or {"mode": "all"}),
        "scope": spec.get("scope") or cfg.get("autoresponder", {}).get("default_scope", "contacts"),
        "trigger": spec.get("trigger") or "view",
        "action": dict(spec.get("action") or {}),
        "limits": dict(spec.get("limits") or {}),
    }
    if spec.get("delay_s"):
        out["delay_s"] = [int(x) for x in spec["delay_s"]][:2]
    if spec.get("valid_until"):
        out["valid_until"] = str(spec["valid_until"])
    if out["stories"].get("mode") not in STORY_MODES:
        raise SystemExit(f"stories.mode must be one of {STORY_MODES}")
    if out["stories"]["mode"] == "ids":
        out["stories"]["ids"] = sorted(int(x) for x in out["stories"].get("ids") or [])
        if not out["stories"]["ids"]:
            raise SystemExit("stories.ids is empty")
    if out["stories"]["mode"] == "tag" and not out["stories"].get("tag"):
        raise SystemExit("stories.tag is required for mode=tag")
    if out["audience"].get("mode") not in AUDIENCE_MODES:
        raise SystemExit(f"audience.mode must be one of {AUDIENCE_MODES}")
    if out["audience"]["mode"] == "users":
        out["audience"]["users"] = [str(u).strip() for u in out["audience"].get("users") or [] if str(u).strip()]
        if not out["audience"]["users"]:
            raise SystemExit("audience.users is empty")
    if out["audience"]["mode"] == "segment" and not out["audience"].get("segment"):
        raise SystemExit("audience.segment is required for mode=segment")
    if out["scope"] not in SCOPES:
        raise SystemExit(f"scope must be one of {SCOPES}")
    if out["trigger"] not in TRIGGERS:
        raise SystemExit(f"trigger must be one of {TRIGGERS}")
    a = out["action"]
    if a.get("type") not in ACTIONS:
        raise SystemExit(f"action.type must be one of {ACTIONS}")
    if a["type"] == "dm":
        variants = [str(v) for v in (a.get("variants") or ([a["text"]] if a.get("text") else [])) if str(v).strip()]
        if not variants:
            raise SystemExit("action.text (or action.variants) is required for a dm rule")
        if any(len(v) > 4000 for v in variants):
            raise SystemExit("a message is longer than 4000 characters")
        a["variants"] = variants
        a.pop("text", None)
    if a["type"] == "segment" and not a.get("segment"):
        raise SystemExit("action.segment is required")
    return out


def digest(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def load_spec(args) -> dict:
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            return json.load(fh)
    if args.json:
        return json.loads(args.json)
    raise SystemExit("pass the rule as --json '{…}' or --file spec.json (see `rule template`)")


def _resolve_users(con, refs) -> list[int]:
    ids = []
    for ref in refs:
        ref = str(ref).strip()
        if ref.lstrip("-").isdigit():
            ids.append(int(ref))
            continue
        uid = user_by_username(con, ref)
        if uid is None:
            raise SystemExit(f"{ref}: not in the database yet (they must have viewed a story at least once)")
        ids.append(uid)
    return ids


# ── matching ───────────────────────────────────────────────────────────────

def _story_matches(con, rule, spec, peer_id, story_id) -> bool:
    st = spec["stories"]
    if st.get("peer", "me") not in ("me", "self", None) and peer_id == db.owner_id(con):
        return False
    mode = st["mode"]
    if mode == "all":
        return True
    if mode == "ids":
        return story_id in st["ids"]
    if mode == "next":
        return rule["bound_story_id"] == story_id
    if mode == "tag":
        row = con.execute("SELECT caption FROM stories WHERE peer_id=? AND story_id=?", (peer_id, story_id)).fetchone()
        return bool(row and row[0] and st["tag"].lower() in row[0].lower())
    return False


def _audience_matches(con, spec, user_id, event) -> bool:
    a = spec["audience"]
    mode = a["mode"]
    if mode == "all":
        return True
    if mode == "users":
        try:
            return user_id in _resolve_users(con, a["users"])
        except SystemExit:
            return False
    if mode == "segment":
        return user_id in segment_members(con, a["segment"])
    if mode == "reacted":
        r = event.get("reaction")
        wanted = a.get("reactions") or []
        return bool(r) and (not wanted or r in wanted)
    if mode == "new":
        first = con.execute("SELECT MIN(first_viewed_at) FROM views WHERE user_id=? AND peer_id=?",
                            (user_id, event["peer_id"])).fetchone()[0]
        return first is not None and first >= (event.get("viewed_at") or 0) - 60
    if mode == "status":
        from . import analytics
        wanted = set(a.get("status") or [])
        for p in analytics.people(con, event["peer_id"]):
            if p["user_id"] == user_id:
                return p["status"] in wanted
        return False
    return False


def user_by_username(con, ref: str) -> int | None:
    """@name → user id, matching the main username and every active collectible username."""
    name = str(ref).strip().lstrip("@").lower()
    if not name:
        return None
    row = con.execute("SELECT user_id FROM people WHERE lower(username)=?", (name,)).fetchone()
    if row:
        return int(row[0])
    for uid, raw in con.execute("SELECT user_id, usernames FROM people WHERE usernames LIKE ?", (f'%"{name}"%',)):
        try:
            if name in {u.lower() for u in json.loads(raw or "[]")}:
                return int(uid)
        except ValueError:
            continue
    return None


def _never(cfg, con) -> set:
    """never_message → ids. An @name nobody in the database has yet resolves later, when that person shows
    up — precheck runs it again for every message, so the ban is never skipped once they are known."""
    refs = cfg.get("autoresponder", {}).get("never_message") or []
    out = set()
    for r in refs:
        r = str(r).strip()
        if r.lstrip("-").isdigit():
            out.add(int(r))
        else:
            uid = user_by_username(con, r)
            if uid is not None:
                out.add(uid)
    return out


def precheck(con, cfg, spec, user_id: int) -> str | None:
    """Cheap, offline exclusions. Returns a skip reason or None.

    Rules that only notify the owner or fill a segment write to nobody, so the
    scope / cooldown / paid-message guards do not apply to them.
    """
    owner = db.owner_id(con)
    if user_id == owner:
        return "owner"
    p = con.execute("SELECT * FROM people WHERE user_id=?", (user_id,)).fetchone()
    if spec.get("action", {}).get("type") != "dm":
        return None if p is not None else "unknown_user"
    if user_id in _never(cfg, con):
        return "never_message"
    if p is None:
        return "unknown_user"
    if p["bot"]:
        return "bot"
    if p["deleted"]:
        return "deleted"
    if (p["paid_stars"] or 0) > 0:
        return "paid_messages"
    if spec["scope"] == "contacts" and not p["is_contact"]:
        return "scope_contacts"
    blocked = con.execute("SELECT 1 FROM views WHERE user_id=? AND (blocked=1 OR hidden_from=1) LIMIT 1",
                          (user_id,)).fetchone()
    if blocked:
        return "blocked"
    cd = int(cfg.get("autoresponder", {}).get("cooldown_days", 7)) * 86400
    recent = con.execute("SELECT 1 FROM deliveries WHERE user_id=? AND status='sent' AND sent_at>? LIMIT 1",
                         (user_id, db.now() - cd)).fetchone()
    if recent:
        return "cooldown"
    return None


def _in_quiet(cfg, ts: int) -> datetime | None:
    """If ts falls into quiet hours, return the moment they end."""
    qh = cfg.get("autoresponder", {}).get("quiet_hours") or []
    if len(qh) != 2:
        return None
    tz = config.get_tz(cfg)
    dt = datetime.fromtimestamp(ts, tz)
    (sh, sm), (eh, em) = [tuple(int(x) for x in s.split(":")) for s in qh]
    start = dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    if (sh, sm) <= (eh, em):
        return end if start <= dt < end else None
    if dt >= start:
        return end + timedelta(days=1)
    if dt < end:
        return end
    return None


def due_time(cfg, spec, ts: int | None = None) -> int:
    ar = cfg.get("autoresponder", {})
    lo, hi = spec.get("delay_s") or ar.get("delay_s") or [120, 360]
    lo = max(int(ar.get("min_delay_s", 30)), int(lo))
    hi = max(lo, int(hi))
    due = (ts or db.now()) + random.randint(lo, hi)
    end = _in_quiet(cfg, due)
    if end is not None:
        due = int(end.timestamp()) + random.randint(0, 900)
    return due


class Engine:
    """Turns collector events into deliveries (queued for active rules, shadow for shadow rules)."""

    def __init__(self, con, cfg):
        self.con = con
        self.cfg = cfg

    def rules(self, statuses=("active", "shadow")):
        q = f"SELECT * FROM rules WHERE status IN ({','.join('?' * len(statuses))})"
        return self.con.execute(q, statuses).fetchall()

    def on_event(self, kind: str, row: dict) -> int:
        if kind == "story":
            self._bind_next(row)
            return 0
        trigger = {"view": "view", "view_changed": "reaction", "reply": "reply"}.get(kind)
        if trigger is None:
            return 0
        if trigger == "reaction" and not row.get("reaction"):
            return 0
        made = 0
        for rule in self.rules():
            spec = json.loads(rule["spec"])
            if spec.get("valid_until") and \
                    datetime.now(config.get_tz(self.cfg)).strftime("%Y-%m-%d") > str(spec["valid_until"])[:10]:
                continue
            want = spec["trigger"]
            # a viewer who reacted at once arrives as a "view" with a reaction
            if not (want == trigger or (want == "reaction" and trigger == "view" and row.get("reaction"))):
                continue
            if not _story_matches(self.con, rule, spec, row["peer_id"], row["story_id"]):
                continue
            if not _audience_matches(self.con, spec, row["user_id"], row):
                continue
            exists = self.con.execute("SELECT 1 FROM deliveries WHERE rule_id=? AND user_id=?",
                                      (rule["id"], row["user_id"])).fetchone()
            if exists:
                continue
            reason = precheck(self.con, self.cfg, spec, row["user_id"])
            status = "shadow" if rule["status"] == "shadow" else ("skipped" if reason else "queued")
            variants = spec["action"].get("variants") or [""]
            variant = row["user_id"] % len(variants)
            self.con.execute(
                "INSERT OR IGNORE INTO deliveries(rule_id,user_id,peer_id,story_id,status,reason,variant,created_at,"
                "due_at,rule_digest) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rule["id"], row["user_id"], row["peer_id"], row["story_id"], status, reason, variant, db.now(),
                 due_time(self.cfg, spec) if status == "queued" else None, rule["digest"]))
            made += 1
        return made

    def _bind_next(self, story_row: dict) -> None:
        """«The next story» means the next one after ACTIVATION: a shadow rule never binds, so a story
        posted while the owner was still looking at the preview does not become the target."""
        for rule in self.con.execute("SELECT * FROM rules WHERE status='active' AND bound_story_id IS NULL"):
            spec = json.loads(rule["spec"])
            if spec["stories"]["mode"] != "next":
                continue
            since = rule["activated_at"] or 0
            if (story_row.get("posted_at") or 0) >= since and story_row.get("peer_id") == db.owner_id(self.con):
                self.con.execute("UPDATE rules SET bound_story_id=? WHERE id=?", (story_row["story_id"], rule["id"]))


# ── segments ───────────────────────────────────────────────────────────────

def segment_members(con, name: str) -> set:
    seg = con.execute("SELECT * FROM segments WHERE name=?", (name,)).fetchone()
    if seg is None:
        return set()
    kind = seg["kind"]
    if kind == "static":
        return {r[0] for r in con.execute("SELECT user_id FROM segment_members WHERE segment=?", (name,))}
    if kind == "contacts":
        return {r[0] for r in con.execute("SELECT user_id FROM people WHERE is_contact=1")}
    if kind == "mutual":
        return {r[0] for r in con.execute("SELECT user_id FROM people WHERE mutual=1")}
    if kind == "close_friends":
        return {r[0] for r in con.execute("SELECT user_id FROM people WHERE close_friend=1")}
    if kind == "status":
        from . import analytics
        return {p["user_id"] for p in analytics.people(con, db.owner_id(con)) if p["status"] == seg["source"]}
    return set()


def segment_cli(cfg, args) -> int:
    con = db.connect(create=False)
    a = args.action
    if a == "list":
        for s in con.execute("SELECT * FROM segments ORDER BY name"):
            print(f"{s['name']}\t{s['kind']}{(':' + s['source']) if s['source'] else ''}\t"
                  f"{len(segment_members(con, s['name']))} people")
        return 0
    if not args.name:
        raise SystemExit("segment name is required")
    if a == "create":
        con.execute("INSERT INTO segments(name,kind,source,created_at) VALUES(?,?,?,?)",
                    (args.name, args.kind, args.source, db.now()))
        if args.members:
            for uid in _resolve_users(con, args.members):
                con.execute("INSERT OR IGNORE INTO segment_members(segment,user_id,added_at) VALUES(?,?,?)",
                            (args.name, uid, db.now()))
        print(f"segment {args.name}: {len(segment_members(con, args.name))} people")
        return 0
    if a in ("add", "remove"):
        seg = con.execute("SELECT kind FROM segments WHERE name=?", (args.name,)).fetchone()
        if seg is None or seg[0] != "static":
            raise SystemExit("only static segments take members")
        for uid in _resolve_users(con, args.members):
            if a == "add":
                con.execute("INSERT OR IGNORE INTO segment_members(segment,user_id,added_at) VALUES(?,?,?)",
                            (args.name, uid, db.now()))
            else:
                con.execute("DELETE FROM segment_members WHERE segment=? AND user_id=?", (args.name, uid))
        print(f"segment {args.name}: {len(segment_members(con, args.name))} people")
        return 0
    if a == "show":
        ids = segment_members(con, args.name)
        for uid in sorted(ids):
            p = con.execute("SELECT first_name,last_name,username FROM people WHERE user_id=?", (uid,)).fetchone()
            name = " ".join(x for x in ((p["first_name"], p["last_name"]) if p else ()) if x)
            print(f"{uid}\t{name}\t{('@' + p['username']) if p and p['username'] else ''}")
        return 0
    if a == "delete":
        con.execute("DELETE FROM segment_members WHERE segment=?", (args.name,))
        con.execute("DELETE FROM segments WHERE name=?", (args.name,))
        print("deleted")
        return 0
    return 1


# ── rule CLI ───────────────────────────────────────────────────────────────

def _summary(con, cfg, rule) -> str:
    spec = json.loads(rule["spec"])
    st, au, ac = spec["stories"], spec["audience"], spec["action"]
    stories = {"ids": f"stories {st.get('ids')}", "next": "the next story you post"
               + (f" (bound: {rule['bound_story_id']})" if rule["bound_story_id"] else ""),
               "tag": f"stories whose caption contains {st.get('tag')!r}", "all": "every story"}[st["mode"]]
    audience = {"all": "everyone", "users": f"only {', '.join(au.get('users', []))}",
                "segment": f"segment {au.get('segment')!r}", "reacted": "people who reacted"
                + (f" with {''.join(au.get('reactions') or [])}" if au.get("reactions") else ""),
                "new": "first-time viewers", "status": f"audience status {au.get('status')}"}[au["mode"]]
    scope = {"contacts": "your contacts only", "dialog": "people who have written to you before",
             "all": "anyone (strangers included)"}[spec["scope"]]
    lines = [f"Rule {rule['id']} — {spec['name']} [{rule['status']}]",
             f"  when: {spec['trigger']} on {stories}",
             f"  who:  {audience}; limited to {scope}"]
    if ac["type"] == "dm":
        for i, v in enumerate(ac["variants"]):
            lines.append(f"  sends{f' (variant {i + 1})' if len(ac['variants']) > 1 else ''}: {v}")
    elif ac["type"] == "notify":
        lines.append("  action: notify you (no message to the viewer)")
    else:
        lines.append(f"  action: add to segment {ac['segment']!r}")
    lim = spec.get("limits") or {}
    ar = cfg.get("autoresponder", {})
    lines.append(f"  limits: rule {lim.get('per_day', '—')}/day, {lim.get('total', '—')} total; account "
                 f"{ar.get('caps', {}).get(spec['scope'])}/day for scope {spec['scope']}, "
                 f"{ar.get('caps', {}).get('per_hour')}/hour; delay {spec.get('delay_s') or ar.get('delay_s')} s; "
                 f"quiet hours {ar.get('quiet_hours')}")
    counts = dict(con.execute("SELECT status, COUNT(*) FROM deliveries WHERE rule_id=? GROUP BY status",
                              (rule["id"],)).fetchall())
    if counts:
        replied = con.execute("SELECT COUNT(*) FROM deliveries WHERE rule_id=? AND replied_at IS NOT NULL",
                              (rule["id"],)).fetchone()[0]
        lines.append("  deliveries: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                     + (f"; replied {replied}" if counts.get("sent") else ""))
    if rule["paused_reason"]:
        lines.append(f"  paused: {rule['paused_reason']}")
    return "\n".join(lines)


def simulate(con, cfg, rule) -> dict:
    """Who would match among already collected views of the targeted stories (no sending)."""
    spec = json.loads(rule["spec"])
    owner = db.owner_id(con)
    st = spec["stories"]
    if st["mode"] == "ids":
        ids = st["ids"]
    elif st["mode"] == "next":
        ids = [rule["bound_story_id"]] if rule["bound_story_id"] else []
    else:
        ids = [r[0] for r in con.execute("SELECT story_id FROM stories WHERE peer_id=? ORDER BY posted_at DESC "
                                         "LIMIT 5", (owner,))]
    match, reasons = [], {}
    seen = set()
    for sid in ids:
        if not _story_matches(con, rule, spec, owner, sid):
            continue
        for v in con.execute("SELECT * FROM views WHERE peer_id=? AND story_id=?", (owner, sid)):
            v = dict(v)
            if v["user_id"] in seen:
                continue
            if spec["trigger"] == "reaction" and not v.get("reaction"):
                continue
            if not _audience_matches(con, spec, v["user_id"], v):
                continue
            seen.add(v["user_id"])
            r = precheck(con, cfg, spec, v["user_id"])
            if r:
                reasons[r] = reasons.get(r, 0) + 1
            else:
                match.append(v["user_id"])
    return {"stories": ids, "would_send": match, "excluded": reasons}


def _drop_queue(con, rule_id: int, reason: str, also_shadow: bool = False) -> int:
    """Queued messages never outlive a change of the rule; shadow records are reset on a new spec."""
    n = con.execute("UPDATE deliveries SET status='skipped', reason=? WHERE rule_id=? AND status='queued'",
                    (reason, rule_id)).rowcount
    if also_shadow:
        con.execute("DELETE FROM deliveries WHERE rule_id=? AND status='shadow'", (rule_id,))
    return n


def cli(cfg, args) -> int:
    con = db.connect(create=False)
    a = args.action
    if a == "template":
        print(json.dumps(TEMPLATE, ensure_ascii=False, indent=2))
        return 0
    if a == "list":
        for r in con.execute("SELECT * FROM rules ORDER BY id"):
            print(_summary(con, cfg, r))
        return 0
    if a == "create":
        spec = normalize(load_spec(args), cfg)
        if spec["audience"]["mode"] == "users":
            _resolve_users(con, spec["audience"]["users"])
        now = db.now()
        cur = con.execute("INSERT INTO rules(name,status,spec,digest,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                          (spec["name"], "shadow", db.dumps(spec), digest(spec), now, now))
        rule = con.execute("SELECT * FROM rules WHERE id=?", (cur.lastrowid,)).fetchone()
        print(_summary(con, cfg, rule))
        print("Created in SHADOW mode: it records who would get the message and sends nothing. "
              f"Next: `rule preview {rule['id']}`.")
        return 0
    if args.id is None:
        raise SystemExit("rule id is required")
    rule = con.execute("SELECT * FROM rules WHERE id=?", (args.id,)).fetchone()
    if rule is None:
        raise SystemExit(f"no rule {args.id}")
    if a == "show":
        print(_summary(con, cfg, rule))
        for d in con.execute("SELECT d.*, p.first_name, p.last_name, p.username FROM deliveries d LEFT JOIN people p "
                             "ON p.user_id=d.user_id WHERE rule_id=? ORDER BY created_at DESC LIMIT 30", (args.id,)):
            name = " ".join(x for x in (d["first_name"], d["last_name"]) if x)
            print(f"  {d['status']:8} {name} {('@' + d['username']) if d['username'] else ''} "
                  f"story {d['story_id']} {d['reason'] or ''}".rstrip())
        return 0
    if a == "update":
        spec = normalize(load_spec(args), cfg)
        con.execute("UPDATE rules SET name=?, spec=?, digest=?, status='shadow', updated_at=?, activated_at=NULL, "
                    "bound_story_id=NULL WHERE id=?", (spec["name"], db.dumps(spec), digest(spec), db.now(), args.id))
        _drop_queue(con, args.id, "rule changed", also_shadow=True)
        rule = con.execute("SELECT * FROM rules WHERE id=?", (args.id,)).fetchone()
        print(_summary(con, cfg, rule))
        print("Updated — back in SHADOW mode; preview and confirm again to activate.")
        return 0
    if a == "preview":
        print(_summary(con, cfg, rule))
        sim = simulate(con, cfg, rule)
        if sim["stories"]:
            print(f"  on already collected views of {sim['stories']}: would send to {len(sim['would_send'])}"
                  + (", excluded: " + ", ".join(f"{k} {v}" for k, v in sim["excluded"].items())
                     if sim["excluded"] else ""))
        if not cfg.get("autoresponder", {}).get("enabled"):
            print("  NOTE: autoresponder.enabled is false in the config — even an active rule sends nothing.")
        print(f"To activate exactly this: `rule activate {rule['id']} --confirm {rule['digest']}`")
        return 0
    if a == "activate":
        if args.confirm != rule["digest"]:
            raise SystemExit("confirmation does not match the current rule — run `rule preview` again and show "
                             "the owner what will be sent")
        is_dm = json.loads(rule["spec"])["action"]["type"] == "dm"
        if is_dm and not cfg.get("autoresponder", {}).get("enabled"):
            raise SystemExit("autoresponder.enabled is false in the config — the owner switches it on there first")
        # the digest is checked again inside the write: an update between preview and activation loses
        cur = con.execute("UPDATE rules SET status='active', activated_at=?, paused_reason=NULL, bound_story_id=NULL "
                          "WHERE id=? AND digest=? AND status IN ('shadow','paused')",
                          (db.now(), args.id, args.confirm))
        if cur.rowcount != 1:
            raise SystemExit("the rule changed or is not in shadow/paused — run `rule preview` again")
        _drop_queue(con, args.id, "rule re-activated", also_shadow=True)
        print(f"Rule {args.id} is ACTIVE. Stop everything at once: `stories.py stop`.")
        return 0
    if a == "shadow":
        con.execute("UPDATE rules SET status='shadow', activated_at=NULL, bound_story_id=NULL WHERE id=?", (args.id,))
        _drop_queue(con, args.id, "rule back in shadow")
        print("shadow")
        return 0
    if a == "pause":
        con.execute("UPDATE rules SET status='paused', paused_reason='by owner' WHERE id=?", (args.id,))
        _drop_queue(con, args.id, "rule paused")
        print("paused")
        return 0
    if a == "delete":
        con.execute("DELETE FROM deliveries WHERE rule_id=? AND status IN ('queued','shadow')", (args.id,))
        con.execute("UPDATE rules SET status='done' WHERE id=?", (args.id,))
        print("done (history of sent messages is kept)")
        return 0
    return 1
