"""How often the service asks Telegram.

Every minute only while something needs it: an active rule (an automatic message, a notification, a
segment that fills itself). With no active rule the service checks every `poll.idle_s` seconds (an hour
by default); activating a rule brings the minute pace back within a minute. `poll.idle_s: 0` keeps the
minute pace always.

What does not need the minute pace: the pulse and the digests re-read their stories right before they
are sent, and tables and the dashboard re-read Telegram first when the data they show is older than five
minutes (`plan`). Replies to stories arrive as messages and are caught at once at any pace.
"""
from __future__ import annotations

from . import db

FRESH_S = 300          # tables and the dashboard re-read Telegram when the last poll is older than this


def idle_s(cfg: dict) -> int:
    return max(0, int((cfg.get("poll") or {}).get("idle_s", 3600) or 0))


def live(con, cfg: dict) -> bool:
    """True when the minute pace is needed."""
    if idle_s(cfg) == 0:
        return True
    return con.execute("SELECT 1 FROM rules WHERE status='active' LIMIT 1").fetchone() is not None


def interval(con, cfg: dict, base: float) -> float:
    """The pause for a task whose full-speed pause is `base` seconds."""
    return base if live(con, cfg) else max(base, idle_s(cfg))


def describe(con, cfg: dict) -> str:
    if live(con, cfg):
        return "checks stories every minute (an active rule needs it)"
    return (f"checks stories every {idle_s(cfg) // 60} min — no active rule; activating one brings the "
            "minute pace back within a minute")


def poll_age_ok(con, cfg: dict, age: int) -> bool:
    """Is the last poll recent enough for the current pace (with ten minutes of slack)?"""
    return age < interval(con, cfg, int((cfg.get("poll") or {}).get("counters_s", 60))) + 600


def stale(con, now: int | None = None) -> bool:
    last = int(db.get_state(con, "last_poll_at", "0") or 0)
    return (now or db.now()) - last > FRESH_S


def channel_ref(con, ref: str | None) -> tuple[int | None, str | None]:
    """--peer of a table → (peer id, what to ask Telegram for). A channel without a username cannot be asked
    for by our stored id; such a table shows the stored data."""
    name = str(ref or "").lstrip("@").lower()
    for row in con.execute("SELECT peer_id, username FROM peers WHERE kind='channel'"):
        if (row["username"] or "").lower() == name or str(row["peer_id"]) == str(ref):
            return int(row["peer_id"]), (f"@{row['username']}" if row["username"] else None)
    return None, (f"@{name}" if name and not name.lstrip("-").isdigit() else None)


def plan(con, target: tuple | None = None, now: int | None = None) -> dict:
    """What a command must re-read before it shows its data. target: ("all", None), ("story", ref) or
    ("channel", ref). The general pass (active stories and final reads) runs when the last poll is old; a story
    that is no longer active is read by itself when its own data is old; a channel table reads that channel."""
    now = now or db.now()
    kind, ref = target or ("all", None)
    out = {"general": False, "story": None, "channel": None}
    if kind == "channel":
        peer_id, ask = channel_ref(con, ref)
        last = int(db.get_state(con, f"channel_polled_at:{peer_id}", "0") or 0) if peer_id else 0
        if ask and now - last > FRESH_S:
            out["channel"] = ask
        return out
    out["general"] = stale(con, now)
    if kind == "story":
        sid = _story_id(con, ref)
        row = con.execute("SELECT expire_at, last_synced FROM stories WHERE peer_id=? AND story_id=?",
                          (db.owner_id(con), sid)).fetchone() if sid else None
        if sid and (row is None or ((row["expire_at"] or 0) <= now and now - (row["last_synced"] or 0) > FRESH_S)):
            out["story"] = sid       # an active one is read by the general pass anyway
    return out


def _story_id(con, ref) -> int | None:
    from . import analytics
    try:
        return analytics.resolve_story(con, db.owner_id(con), ref)
    except SystemExit:
        return None


def data_time(con, target: tuple | None = None) -> int:
    """When the data a command shows was last read from Telegram (0 = never)."""
    kind, ref = target or ("all", None)
    if kind == "channel":
        peer_id, _ = channel_ref(con, ref)
        return int(db.get_state(con, f"channel_polled_at:{peer_id}", "0") or 0) if peer_id else 0
    if kind == "story":
        sid = _story_id(con, ref)
        row = con.execute("SELECT last_synced FROM stories WHERE peer_id=? AND story_id=?",
                          (db.owner_id(con), sid)).fetchone() if sid else None
        if row and row[0]:
            return int(row[0])
    return int(db.get_state(con, "last_poll_at", "0") or 0)
