"""Pulse (a fresh story vs your usual) and digests (weekly/monthly), as Markdown."""
from __future__ import annotations

from datetime import datetime

from . import analytics, config, db
from .i18n import t
from .tables import MEDIA_ICON, cut, fmt_date, fmt_index, md_escape, name_cell, nick_cell, people_table, \
    summary_table


def pulse(con, cfg: dict, peer_id: int, story_id: int) -> dict | None:
    hours = float(cfg.get("pulse", {}).get("after_hours", 2))
    n = int(cfg.get("pulse", {}).get("baseline_stories", 20))
    s = con.execute("SELECT * FROM stories WHERE peer_id=? AND story_id=?", (peer_id, story_id)).fetchone()
    if s is None or not s["posted_at"]:
        return None
    now_views = analytics.story_at(con, peer_id, story_id, hours)
    base = analytics.baseline_at(con, peer_id, s["posted_at"], hours, n)
    if now_views is None:
        return None
    return {"story": dict(s), "hours": hours, "views": now_views, "baseline": base,
            "index": (now_views / base) if base else None}


def pulse_text(con, cfg: dict, story_ref=None, peer_id: int | None = None) -> str:
    peer_id = peer_id or db.owner_id(con)
    sid = analytics.resolve_story(con, peer_id, story_ref or "last")
    p = pulse(con, cfg, peer_id, sid)
    tz = config.get_tz(cfg)
    if p is None:
        return t(cfg, "no_data")
    s = p["story"]
    ix = p["index"]
    if ix is None:
        verdict = ""
    elif ix >= 1.1:
        verdict = f"📈 {fmt_index(ix)} {t(cfg, 'pulse_up')}"
    elif ix <= 0.9:
        verdict = f"📉 {fmt_index(ix)} {t(cfg, 'pulse_down')}"
    else:
        verdict = f"➖ {t(cfg, 'pulse_flat')}"
    H = t(cfg, "h")
    hours = int(p["hours"]) if float(p["hours"]).is_integer() else p["hours"]
    icon = MEDIA_ICON.get(s.get("media_kind") or "", "")
    lines = [f"**{t(cfg, 'pulse')} · {t(cfg, 'story_header')} {sid}** · "
             f"{fmt_date(s['posted_at'], tz)} {datetime.fromtimestamp(s['posted_at'], tz):%H:%M} {icon}".strip()]
    if s.get("caption"):
        lines.append("_" + md_escape(cut(s["caption"], 80)) + "_")
    base = f" · {t(cfg, 'median').lower()} {round(p['baseline'])}" if p["baseline"] is not None else ""
    lines.append(f"{hours}{H}: 👁 {p['views']}{base}" + (f" · {verdict}" if verdict else ""))
    reactions = s.get("reactions") or 0
    lines.append(f"❤ {reactions}")
    return "\n".join(lines)


def digest_text(con, cfg: dict, period: str = "7d", peer_id: int | None = None) -> str:
    peer_id = peer_id or db.owner_id(con)
    tz = config.get_tz(cfg)
    start, end = analytics.parse_period(period, None, None, tz)
    tb = summary_table(con, cfg, peer_id, start, end)
    tb.title = tb.title.replace(t(cfg, "summary_header"), t(cfg, "digest"))
    tb.rows.sort(key=lambda r: -(r["views"] if isinstance(r["views"], int) else 0))
    tb.rows = tb.rows[:10]
    parts = [tb.to_md()]
    ppl = analytics.people(con, peer_id)
    new_core = [p for p in ppl if p["status"] == "core" and p.get("first") and p["first"] >= start - 60 * 86400][:5]
    cooling = [p for p in ppl if p["status"] == "cooling"][:5]
    if new_core:
        parts.append(f"**{t(cfg, 'new_core')}:** " + ", ".join(
            f"{name_cell(p, cfg)} {nick_cell(p, cfg) if p.get('username') else ''}".strip() for p in new_core))
    if cooling:
        parts.append(f"**{t(cfg, 'cooling_people')}:** " + ", ".join(
            f"{name_cell(p, cfg)} {nick_cell(p, cfg) if p.get('username') else ''}".strip() for p in cooling))
    h = analytics.hours(con, peer_id, end - 90 * 86400, end, tz)
    if h["best_post_hours"]:
        parts.append(f"**{t(cfg, 'best_hours')}:** " + ", ".join(
            f"{hh:02d}:00 (👁 {round(med)})" for hh, med, _ in h["best_post_hours"]))
    return "\n\n".join(parts)


def due_digests(con, cfg: dict, now_ts: int | None = None) -> list[tuple[str, str]]:
    """(key, period) pairs that should be sent now and were not sent yet."""
    d = cfg.get("digest", {})
    if not d.get("enabled", True):
        return []
    tz = config.get_tz(cfg)
    now = datetime.fromtimestamp(now_ts or db.now(), tz)
    hh, mm = (int(x) for x in str(d.get("time", "20:00")).split(":"))
    if (now.hour, now.minute) < (hh, mm):
        return []
    out = []
    if now.weekday() == int(d.get("weekday", 6)):
        out.append((f"digest-week-{now:%G-%V}", "7d"))
    if now.day == int(d.get("monthly_day", 1)):
        out.append((f"digest-month-{now:%Y-%m}", "30d"))
    sent = {r[0] for r in con.execute("SELECT key FROM notifications")}
    return [(k, p) for k, p in out if k not in sent]


def mark_sent(con, key: str) -> None:
    con.execute("INSERT OR REPLACE INTO notifications(key, sent_at) VALUES(?,?)", (key, db.now()))


def due_pulses(con, cfg: dict, peer_id: int, now_ts: int | None = None) -> list[int]:
    p = cfg.get("pulse", {})
    if not p.get("enabled", True):
        return []
    now = now_ts or db.now()
    after = int(float(p.get("after_hours", 2)) * 3600)
    rows = con.execute("SELECT story_id FROM stories WHERE peer_id=? AND deleted=0 AND pulse_sent_at IS NULL "
                       "AND list_available IS NOT 0 AND posted_at<=? AND posted_at>?",
                       (peer_id, now - after, now - after - 6 * 3600)).fetchall()
    return [r[0] for r in rows]


def people_report(con, cfg, peer_id, status):
    return people_table(con, cfg, peer_id, status).to_md()
