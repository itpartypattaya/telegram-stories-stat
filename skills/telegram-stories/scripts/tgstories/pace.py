"""How often the service asks Telegram.

Every minute only while something needs it: an active rule (an automatic message, a notification, a
segment that fills itself). With no active rule the service checks every `poll.idle_s` seconds (an hour
by default); activating a rule brings the minute pace back within a minute. `poll.idle_s: 0` keeps the
minute pace always.

What does not need the minute pace: the pulse and the digests re-read their stories right before they
are sent, and tables and the dashboard re-read Telegram first when the data is older than five minutes.
Replies to stories arrive as messages and are caught at once at any pace.
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
