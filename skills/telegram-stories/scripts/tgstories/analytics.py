"""Metrics over the collected data. Definitions: references/metrics.md."""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta

from . import db

DAY = 86400
HOUR = 3600


# ── periods and references ────────────────────────────────────────────────

def parse_period(period: str | None, date_from: str | None, date_to: str | None, tz) -> tuple[int, int]:
    end = db.now()
    if date_to:
        end = int((datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)).timestamp())
    if date_from:
        start = int(datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=tz).timestamp())
        return start, end
    period = (period or "30d").strip().lower()
    if period == "all":
        return 0, end
    m = re.fullmatch(r"(\d+)\s*([dwmy])", period)
    if not m:
        raise SystemExit(f"period '{period}' — use forms like 7d, 4w, 6m, 1y or all")
    n, unit = int(m.group(1)), m.group(2)
    days = n * {"d": 1, "w": 7, "m": 30, "y": 365}[unit]
    return end - days * DAY, end


def resolve_story(con, peer_id: int, ref: str | int | None) -> int:
    ref = str(ref if ref is not None else "last").strip()
    if ref in ("last", "latest", "0"):
        ref = "-1"
    if ref.startswith("-") and ref[1:].isdigit():
        n = int(ref[1:])
        row = con.execute("SELECT story_id FROM stories WHERE peer_id=? AND deleted=0 AND posted_at IS NOT NULL "
                          "ORDER BY posted_at DESC LIMIT 1 OFFSET ?", (peer_id, n - 1)).fetchone()
        if not row:
            raise SystemExit("no such story — run `stories.py backfill` or `stories.py sync` first")
        return int(row[0])
    if ref.isdigit():
        return int(ref)
    raise SystemExit(f"story reference '{ref}' — use an id, `last` or -N")


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


# ── one story ──────────────────────────────────────────────────────────────

def story(con, peer_id: int, story_id: int, baseline_n: int = 20) -> dict:
    s = con.execute("SELECT * FROM stories WHERE peer_id=? AND story_id=?", (peer_id, story_id)).fetchone()
    if s is None:
        raise SystemExit(f"story {story_id} is not in the database")
    rows = con.execute(
        """SELECT v.*, p.first_name, p.last_name, p.username, p.is_contact, p.mutual, p.close_friend,
                  p.bot, p.deleted AS p_deleted,
                  NOT EXISTS (SELECT 1 FROM views w WHERE w.user_id=v.user_id AND w.peer_id=v.peer_id
                              AND w.first_viewed_at < v.first_viewed_at) AS first_time
           FROM views v LEFT JOIN people p ON p.user_id=v.user_id
           WHERE v.peer_id=? AND v.story_id=? ORDER BY v.first_viewed_at""", (peer_id, story_id)).fetchall()
    posted = s["posted_at"] or 0
    lags = [(r["first_viewed_at"] or posted) - posted for r in rows]
    at = {h: sum(1 for x in lags if x <= h * HOUR) for h in (1, 2, 6, 24, 48)}
    replies = con.execute("SELECT COUNT(*) FROM story_replies WHERE peer_id=? AND story_id=?",
                          (peer_id, story_id)).fetchone()[0]
    comp = {"mutual": 0, "contact": 0, "other": 0, "close_friend": 0}
    for r in rows:
        if r["mutual"]:
            comp["mutual"] += 1
        elif r["is_contact"]:
            comp["contact"] += 1
        else:
            comp["other"] += 1
        if r["close_friend"]:
            comp["close_friend"] += 1
    views = s["views"] if s["views"] is not None else len(rows)
    base, views_for_index = fair_baseline(con, peer_id, s, views, len(rows), baseline_n)
    tail = sum(1 for x in lags if x > 48 * HOUR)
    return {
        "story": dict(s),
        "viewers": [dict(r) for r in rows],
        "views": views,
        "listed": len(rows),
        "reactions": s["reactions"] if s["reactions"] is not None else sum(1 for r in rows if r["reaction"]),
        "forwards": s["forwards"] or 0,
        "replies": replies,
        "at": at,
        "t50": sorted(lags)[len(lags) // 2] if lags else None,
        "composition": comp,
        "new_viewers": sum(1 for r in rows if r["first_time"]),
        "baseline": base,
        "index": (views_for_index / base) if base else None,
        "young": young(s),
        "tail_after_48h": tail,
        "list_available": s["list_available"],
    }


def young(s) -> bool:
    """Still inside its first 48 hours: its counter is not final yet."""
    return bool(s["posted_at"]) and db.now() - s["posted_at"] < 48 * HOUR


def fair_baseline(con, peer_id: int, s, views: int, listed: int, n: int = 20) -> tuple:
    """(baseline, value to compare). A story younger than 48 h is compared with earlier stories at the
    same age (from their viewer timestamps); a finished one — with their final counters."""
    if young(s):
        if s["list_available"] != 1:
            return None, views   # no list yet (or none at all): no honest same-age comparison — show nothing
        age_h = max(0.25, (db.now() - s["posted_at"]) / HOUR)
        return baseline_at(con, peer_id, s["posted_at"], age_h, n), listed
    prev = con.execute("SELECT views FROM stories WHERE peer_id=? AND deleted=0 AND posted_at<? "
                       "AND views IS NOT NULL ORDER BY posted_at DESC LIMIT ?", (peer_id, s["posted_at"], n)).fetchall()
    return median([p[0] for p in prev]), views


def story_at(con, peer_id: int, story_id: int, hours: float) -> int | None:
    s = con.execute("SELECT posted_at, list_available, list_synced FROM stories WHERE peer_id=? AND story_id=?",
                    (peer_id, story_id)).fetchone()
    # unknown is not zero: a list never read (list_synced NULL) or not given by Telegram yields None
    if s is None or not s["posted_at"] or s["list_available"] != 1 or s["list_synced"] is None:
        return None
    return con.execute("SELECT COUNT(*) FROM views WHERE peer_id=? AND story_id=? AND first_viewed_at<=?",
                       (peer_id, story_id, s["posted_at"] + int(hours * HOUR))).fetchone()[0]


def baseline_at(con, peer_id: int, before_ts: int, hours: float, n: int = 20) -> float | None:
    prev = con.execute("SELECT story_id FROM stories WHERE peer_id=? AND deleted=0 AND posted_at<? "
                       "AND list_available=1 AND posted_at<? ORDER BY posted_at DESC LIMIT ?",
                       (peer_id, before_ts, db.now() - int(hours * HOUR), n)).fetchall()
    return median([story_at(con, peer_id, p[0], hours) for p in prev])


# ── many stories ───────────────────────────────────────────────────────────

def stories_in(con, peer_id: int, start: int, end: int) -> list[dict]:
    rows = con.execute("SELECT * FROM stories WHERE peer_id=? AND deleted=0 AND posted_at>=? AND posted_at<? "
                       "ORDER BY posted_at", (peer_id, start, end)).fetchall()
    out = []
    for s in rows:
        listed = s["viewers_listed"] or 0
        first_hour = story_at(con, peer_id, s["story_id"], 1)
        replies = con.execute("SELECT COUNT(*) FROM story_replies WHERE peer_id=? AND story_id=?",
                              (peer_id, s["story_id"])).fetchone()[0]
        views = s["views"] if s["views"] is not None else listed
        base, compare = fair_baseline(con, peer_id, s, views, listed)
        out.append({**dict(s), "views": views, "replies": replies,
                    "first_hour": first_hour, "first_hour_share": (first_hour / listed) if listed and
                    first_hour is not None else None,
                    "index": (compare / base) if base else None, "young": young(s)})
    return out


def summary(con, peer_id: int, start: int, end: int) -> dict:
    items = stories_in(con, peer_id, start, end)
    views = [i["views"] for i in items if i["views"] is not None]
    unique = con.execute("SELECT COUNT(DISTINCT user_id) FROM views WHERE peer_id=? AND first_viewed_at>=? "
                         "AND first_viewed_at<?", (peer_id, start, end)).fetchone()[0]
    new = con.execute("""SELECT COUNT(*) FROM (SELECT user_id, MIN(first_viewed_at) f FROM views WHERE peer_id=?
                         GROUP BY user_id) WHERE f>=? AND f<?""", (peer_id, start, end)).fetchone()[0]
    lost = con.execute("""SELECT COUNT(*) FROM (
                            SELECT user_id FROM views WHERE peer_id=? AND first_viewed_at>=? AND first_viewed_at<?
                            GROUP BY user_id HAVING COUNT(*)>=2)
                          WHERE user_id NOT IN (SELECT user_id FROM views WHERE peer_id=? AND first_viewed_at>=?
                                                AND first_viewed_at<?)""",
                       (peer_id, start - 30 * DAY, start, peer_id, start, end)).fetchone()[0] if start else 0
    active30 = con.execute("SELECT COUNT(DISTINCT user_id) FROM views WHERE peer_id=? AND first_viewed_at>=? "
                           "AND first_viewed_at<?", (peer_id, end - 30 * DAY, end)).fetchone()[0]
    return {
        "items": items, "count": len(items), "views_total": sum(views), "views_median": median(views),
        "reactions_total": sum(i["reactions"] or 0 for i in items),
        "replies_total": sum(i["replies"] for i in items),
        "unique_viewers": unique, "new_viewers": new, "lost_viewers": lost, "active_30d": active30,
        "start": start, "end": end,
    }


# ── people ─────────────────────────────────────────────────────────────────

STATUS_ORDER = ["core", "regular", "new", "occasional", "cooling", "lost"]


def people(con, peer_id: int, now: int | None = None) -> list[dict]:
    now = now or db.now()
    recent = con.execute("SELECT story_id, posted_at FROM stories WHERE peer_id=? AND deleted=0 "
                         "AND list_available=1 ORDER BY posted_at DESC LIMIT 40", (peer_id,)).fetchall()
    last20 = recent[:20]
    prev20 = recent[20:40]
    last_ids = {r[0] for r in last20}
    prev_ids = {r[0] for r in prev20}
    posted = {r[0]: r[1] for r in recent}
    by_user: dict = {}
    for r in con.execute("SELECT v.user_id, v.story_id, v.first_viewed_at, v.reaction, s.posted_at, v.viewed_at "
                         "FROM views v JOIN stories s ON s.peer_id=v.peer_id AND s.story_id=v.story_id "
                         "WHERE v.peer_id=?", (peer_id,)):
        u = by_user.setdefault(r[0], {"stories": set(), "lags": [], "first": None, "last": None, "reactions": 0})
        u["stories"].add(r[1])
        ts, latest = r[2], r[5] or r[2]
        if ts is not None:
            # first contact = earliest first view; last activity = latest view (a re-view of an old
            # profile story today counts as being active today)
            u["first"] = ts if u["first"] is None else min(u["first"], ts)
            u["last"] = latest if u["last"] is None else max(u["last"], latest)
            if r[1] in last_ids and r[4]:
                u["lags"].append(ts - r[4])
        if r[3]:
            u["reactions"] += 1
    replies = dict(con.execute("SELECT user_id, COUNT(*) FROM story_replies WHERE peer_id=? GROUP BY user_id",
                               (peer_id,)).fetchall())
    profiles = {r["user_id"]: dict(r) for r in con.execute("SELECT * FROM people")}
    out = []
    for uid, u in by_user.items():
        first = u["first"] or now
        eligible = [sid for sid in last_ids if (posted.get(sid) or 0) >= first - 2 * DAY] or list(last_ids)
        seen20 = len(u["stories"] & set(eligible))
        share = seen20 / len(eligible) if eligible else 0.0
        prev_share = (len(u["stories"] & prev_ids) / len(prev_ids)) if prev_ids else 0.0
        lag = median(u["lags"])
        last = u["last"] or 0
        if first >= now - 14 * DAY:
            status = "new"
        elif last < now - 60 * DAY:
            status = "lost"
        elif last < now - 21 * DAY and max(prev_share, share) >= 0.4:
            status = "cooling"
        elif share >= 0.7 and lag is not None and lag <= 3 * HOUR:
            status = "core"
        elif share >= 0.4:
            status = "regular"
        else:
            status = "occasional"
        p = profiles.get(uid, {})
        out.append({**p, "user_id": uid, "seen_total": len(u["stories"]), "seen_last20": seen20,
                    "eligible_last20": len(eligible), "share": share, "lag": lag, "first": u["first"],
                    "last": u["last"], "reactions": u["reactions"], "replies": replies.get(uid, 0),
                    "status": status})
    out.sort(key=lambda x: (STATUS_ORDER.index(x["status"]), -x["share"], -(x["last"] or 0)))
    return out


# ── time of day ────────────────────────────────────────────────────────────

def hours(con, peer_id: int, start: int, end: int, tz) -> dict:
    by_hour = [0] * 24
    by_day = [0] * 7
    for (ts,) in con.execute("SELECT first_viewed_at FROM views WHERE peer_id=? AND first_viewed_at>=? "
                             "AND first_viewed_at<?", (peer_id, start, end)):
        dt = datetime.fromtimestamp(ts, tz)
        by_hour[dt.hour] += 1
        by_day[dt.weekday()] += 1
    # posting hour → median reach of stories posted at that hour (3+ stories)
    post: dict = {}
    for s in con.execute("SELECT posted_at, views FROM stories WHERE peer_id=? AND deleted=0 AND posted_at>=? "
                         "AND posted_at<? AND views IS NOT NULL", (peer_id, start, end)):
        h = datetime.fromtimestamp(s[0], tz).hour
        post.setdefault(h, []).append(s[1])
    best = sorted(((h, median(v), len(v)) for h, v in post.items() if len(v) >= 3),
                  key=lambda x: -(x[1] or 0))[:3]
    return {"by_hour": by_hour, "by_day": by_day, "best_post_hours": best, "total": sum(by_hour)}


def channel(con, peer_id: int, start: int, end: int) -> list[dict]:
    rows = con.execute("SELECT * FROM stories WHERE peer_id=? AND deleted=0 AND posted_at>=? AND posted_at<? "
                       "ORDER BY posted_at", (peer_id, start, end)).fetchall()
    out = []
    for s in rows:
        inter = dict(con.execute("SELECT kind, COUNT(*) FROM public_interactions WHERE peer_id=? AND story_id=? "
                                 "GROUP BY kind", (peer_id, s["story_id"])).fetchall())
        out.append({**dict(s), "named_reactions": inter.get("reaction", 0),
                    "public_forwards": inter.get("forward", 0) + inter.get("repost", 0)})
    return out
