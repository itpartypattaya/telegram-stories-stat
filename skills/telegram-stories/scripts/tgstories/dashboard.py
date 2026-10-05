"""One-file HTML dashboard: the tables' numbers on one page, with charts.

The page is static: no server, no network requests (its Content-Security-Policy forbids them), every number
inside the file. All metrics come from analytics.py, the same source as the tables. Charts are drawn here as
SVG, so the page reads without JavaScript; a little script sorts tables, filters people and opens viewer
lists from the embedded data.
"""
from __future__ import annotations

import base64
import html
import json
import math
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from . import __version__, analytics, config, db
from .i18n import t
from .tables import MEDIA_ICON, fmt_lag, full_name, reaction_cell, who_cell

DAY = 86400
PRESETS = [("30d", 30), ("90d", 90), ("1y", 365), ("all", None)]
TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "dashboard.html"
THUMB_MAX_BYTES = 200_000
GALLERY = 6
STATUS_COLOR = {s: f"var(--st-{s})" for s in analytics.STATUS_ORDER}


def e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def fmt_num(n, cfg) -> str:
    if n is None:
        return "—"
    if isinstance(n, float) and not n.is_integer():
        n = round(n)
    s = f"{int(n):,}"
    return s.replace(",", " ") if cfg.get("locale") == "ru" else s


def fmt_pct(x) -> str:
    return "" if x is None else f"{round(x * 100)}%"


def fmt_index(ix) -> str:
    return "" if ix is None else f"{round((ix - 1) * 100):+d}%"


def index_class(ix) -> str:
    if ix is None:
        return ""
    return "up" if ix >= 1.1 else ("down" if ix <= 0.9 else "")


def lag_text(seconds, cfg) -> str:
    return fmt_lag(seconds, cfg) or "—"


def axis(top_value: float, steps: int = 5) -> tuple[int, int]:
    """(top, step) of a value axis with round ticks: 0, 50, 100… — never 62 or 188."""
    raw = max(1.0, top_value / steps)
    exp = 10 ** math.floor(math.log10(raw))
    step = next(m * exp for m in (1, 2, 5, 10) if m * exp >= raw)
    step = max(1, int(step))
    return step * max(1, math.ceil(top_value / step)), step


def grid_lines(top: int, step: int, y, L: int, right: int, cfg) -> list[str]:
    return [f'<line class="gl" x1="{L}" x2="{right}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
            f'<text x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{e(fmt_num(v, cfg))}</text>'
            for v in range(0, top + 1, step)]


def _date(ts, tz, fmt="%d.%m.%Y") -> str:
    return datetime.fromtimestamp(ts, tz).strftime(fmt) if ts else ""


# ── data ───────────────────────────────────────────────────────────────────

def collect(con, cfg: dict, *, now: int | None = None) -> dict:
    """Every number the page shows, computed by analytics.py."""
    tz = config.get_tz(cfg)
    peer = db.owner_id(con)
    if not peer:
        raise SystemExit("owner unknown — run `stories.py login` first")
    now = now or db.now()
    owner = con.execute("SELECT title, username FROM peers WHERE peer_id=?", (peer,)).fetchone()
    periods = []
    for key, days in PRESETS:
        start = now - days * DAY if days else 0
        periods.append({
            "key": key, "days": days, "start": start,
            "summary": analytics.summary(con, peer, start, now),
            "prev": analytics.summary(con, peer, start - days * DAY, start) if days else None,
            "hours": analytics.hours(con, peer, start, now, tz),
            "speed": analytics.speed(con, peer, start, now),
        })
    channels = []
    for row in con.execute("SELECT peer_id, title, username FROM peers WHERE kind='channel' AND tracked=1 "
                           "ORDER BY title"):
        channels.append({"peer_id": row["peer_id"], "title": row["title"], "username": row["username"],
                         "stories": analytics.channel(con, row["peer_id"], 0, now + 1)})
    counts: dict = {}
    for r in con.execute("SELECT rule_id, status, COUNT(*) n, SUM(replied_at IS NOT NULL) replied "
                         "FROM deliveries GROUP BY rule_id, status"):
        c = counts.setdefault(r["rule_id"], {"replied": 0})
        c[r["status"]] = r["n"]
        c["replied"] += r["replied"] or 0
    rules = []
    for r in con.execute("SELECT id, name, status, spec FROM rules ORDER BY id"):
        try:
            spec = json.loads(r["spec"])
        except (TypeError, ValueError):
            spec = {}
        rules.append({"id": r["id"], "name": r["name"], "status": r["status"], "spec": spec,
                      "counts": counts.get(r["id"], {"replied": 0})})
    return {
        "peer_id": peer, "owner": dict(owner) if owner else {}, "now": now, "tz": tz,
        "last_poll": int(db.get_state(con, "last_poll_at", "0") or 0),
        "periods": periods,
        "monthly": analytics.monthly(con, peer, tz),
        "people": analytics.people(con, peer, now),
        "channels": channels,
        "rules": rules,
        "auto_enabled": bool(cfg.get("autoresponder", {}).get("enabled")),
        "kill_switch": str(db.get_state(con, "kill_switch", "0")) == "1",
        "views": con.execute(
            "SELECT v.story_id, v.user_id, v.first_viewed_at, v.reaction, s.posted_at FROM views v "
            "JOIN stories s ON s.peer_id=v.peer_id AND s.story_id=v.story_id "
            "WHERE v.peer_id=? AND s.deleted=0 ORDER BY v.story_id, v.first_viewed_at", (peer,)).fetchall(),
    }


def default_period(data: dict, wanted: str | None) -> str:
    keys = [p["key"] for p in data["periods"]]
    if wanted in keys:
        return wanted
    for p in data["periods"]:
        if p["summary"]["count"] >= 10:
            return p["key"]
    return "all"


# ── charts (SVG) ───────────────────────────────────────────────────────────

def bars_svg(items: list, cfg: dict, tz) -> str:
    """Views per story, in posting order. Color: against the usual (the story's index)."""
    W, H, L, R, TOP, B = 960, 250, 46, 74, 10, 26
    vals = [(i.get("views") or 0) for i in items]
    top, step = axis(max(vals or [1]))
    pw, ph = W - L - R, H - TOP - B
    slot = pw / max(1, len(items))
    bw = max(1.0, min(26.0, slot * 0.72))
    y = lambda v: TOP + ph - (v / top) * ph  # noqa: E731
    out = [f'<div class="cw"><svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{e(t(cfg, "d_per_story"))}">']
    out += grid_lines(top, step, y, L, W - R, cfg)
    for n, (it, v) in enumerate(zip(items, vals)):
        x = L + n * slot + (slot - bw) / 2
        ix = it.get("index")
        cls = "by" if it.get("young") else {"up": "bu", "down": "bd"}.get(index_class(ix), "bf")
        tip = (f"{t(cfg, 'story')} {it['story_id']} · {_date(it['posted_at'], tz)} · 👁 {fmt_num(v, cfg)}"
               + (f" · {t(cfg, 'index')} {fmt_index(ix)}" if ix is not None else "")
               + (f" · {t(cfg, 'd_young')}" if it.get("young") else "")
               + (f"\n{(it.get('caption') or '')[:120]}" if it.get("caption") else ""))
        h = max(1.0, TOP + ph - y(v))
        out.append(f'<rect class="{cls}" data-story="{it["story_id"]}" x="{x:.1f}" y="{TOP + ph - h:.1f}" '
                   f'width="{bw:.1f}" height="{h:.1f}" rx="{min(3, bw / 3):.1f}"><title>{e(tip)}</title></rect>')
    med = analytics.median(vals)
    if med and len(items) > 1:
        out.append(f'<line class="ml" x1="{L}" x2="{W - R}" y1="{y(med):.1f}" y2="{y(med):.1f}"/>'
                   f'<text x="{W - R + 6}" y="{y(med) - 2:.1f}">{e(t(cfg, "d_median_line"))}</text>'
                   f'<text x="{W - R + 6}" y="{y(med) + 11:.1f}">{e(fmt_num(med, cfg))}</text>')
    ticks = min(len(items), 7)
    for k in range(ticks):
        n = round(k * (len(items) - 1) / max(1, ticks - 1))
        x = L + n * slot + slot / 2
        anchor = "start" if k == 0 and ticks > 1 else ("end" if k == ticks - 1 and ticks > 1 else "middle")
        out.append(f'<text x="{x:.1f}" y="{H - 6}" text-anchor="{anchor}">'
                   f'{e(_date(items[n]["posted_at"], tz, "%d.%m.%y"))}</text>')
    out.append("</svg></div>")
    legend = (f'<div class="legend"><span><i style="background:var(--up)"></i>{e(t(cfg, "pulse_up"))} (+10%)</span>'
              f'<span><i style="background:var(--flat)"></i>{e(t(cfg, "pulse_flat"))}</span>'
              f'<span><i style="background:var(--down)"></i>{e(t(cfg, "pulse_down"))} (−10%)</span>'
              f'<span><i style="background:var(--young)"></i>{e(t(cfg, "d_young"))}</span>'
              f'<span><i class="line"></i>{e(t(cfg, "d_median_line"))}</span></div>')
    return "".join(out) + legend


def trend_svg(months: list, cfg: dict) -> str:
    W, H, L, R, TOP, B = 960, 250, 46, 10, 10, 26
    if not months:
        return f'<p class="empty">{e(t(cfg, "no_data"))}</p>'
    top, step = axis(max([m["active"] for m in months] + [m["views_median"] or 0 for m in months] + [1]))
    pw, ph = W - L - R, H - TOP - B
    slot = pw / len(months)
    bw = max(2.0, min(34.0, slot * 0.7))
    y = lambda v: TOP + ph - (v / top) * ph  # noqa: E731
    out = [f'<div class="cw"><svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{e(t(cfg, "d_trend"))}">']
    out += grid_lines(top, step, y, L, W - R, cfg)
    pts = []
    for n, m in enumerate(months):
        x = L + n * slot + (slot - bw) / 2
        label = f"{m['month']:02d}.{m['year']}"
        tip = (f"{label} · {t(cfg, 'stories')}: {m['stories']} · {t(cfg, 'd_active')}: {fmt_num(m['active'], cfg)} · "
               f"{t(cfg, 'd_new')}: {fmt_num(m['new'], cfg)}"
               + (f" · {t(cfg, 'd_story_median')}: {fmt_num(m['views_median'], cfg)}"
                  if m["views_median"] is not None else ""))
        ha, hn = TOP + ph - y(m["active"]), TOP + ph - y(min(m["new"], m["active"]))
        out.append(f'<g class="mo"><title>{e(tip)}</title>'
                   f'<rect class="ba" x="{x:.1f}" y="{TOP + ph - ha:.1f}" width="{bw:.1f}" height="{ha:.1f}" rx="2"/>'
                   f'<rect class="bn" x="{x:.1f}" y="{TOP + ph - hn:.1f}" width="{bw:.1f}" height="{hn:.1f}" rx="2"/>'
                   f'</g>')
        if m["views_median"] is not None:
            pts.append((L + n * slot + slot / 2, y(m["views_median"])))
    if len(pts) > 1:
        out.append('<polyline class="ln" points="' + " ".join(f"{px:.1f},{py:.1f}" for px, py in pts) + '"/>')
    out += [f'<circle class="dt" cx="{px:.1f}" cy="{py:.1f}" r="2.6"/>' for px, py in pts]
    every = max(1, math.ceil(len(months) / 12))
    for n, m in enumerate(months):
        if n % every == 0:
            out.append(f'<text x="{L + n * slot + slot / 2:.1f}" y="{H - 6}" text-anchor="middle">'
                       f'{m["month"]:02d}.{m["year"] % 100:02d}</text>')
    out.append("</svg></div>")
    legend = (f'<div class="legend"><span><i style="background:rgb(var(--accent-rgb) / .28)"></i>'
              f'{e(t(cfg, "d_active"))}</span><span><i style="background:var(--accent)"></i>{e(t(cfg, "d_new"))}'
              f'</span><span><i class="line"></i>{e(t(cfg, "d_story_median"))}</span></div>')
    return "".join(out) + legend


def heatmap(h: dict, cfg: dict, tz_label: str) -> str:
    peak = max((max(r) for r in h["matrix"]), default=0) or 1
    days = t(cfg, "weekdays")
    out = ['<div class="heat-wrap"><div class="heat"><div class="hh"></div>']
    out += [f'<div class="hh">{hr:02d}</div>' if hr % 3 == 0 else '<div class="hh"></div>' for hr in range(24)]
    total = h["total"] or 1
    for d, row in enumerate(h["matrix"]):
        out.append(f'<div class="hl">{e(days[d])}</div>')
        for hr, n in enumerate(row):
            a = 0.06 + 0.94 * (n / peak) if n else 0.04
            out.append(f'<div class="hc" style="--a:{a:.2f}" title="{e(days[d])} {hr:02d}:00 · 👁 '
                       f'{e(fmt_num(n, cfg))} ({round(n * 100 / total)}%)"></div>')
    out.append("</div></div>")
    note = f'<p class="note">{e(t(cfg, "d_heat_note").replace("{tz}", tz_label))}</p>'
    return note + "".join(out)


def best_hours(h: dict, cfg: dict) -> str:
    if not h["best_post_hours"]:
        return f'<p class="empty">{e(t(cfg, "no_data"))}</p>'
    return '<div class="hours">' + "".join(
        f'<span class="chip"><b>{hh:02d}:00</b> · 👁 {e(fmt_num(med, cfg))} · n={n}</span>'
        for hh, med, n in h["best_post_hours"]) + "</div>"


def speed_block(sp: dict, cfg: dict) -> str:
    if not sp["stories"]:
        return f'<p class="empty">{e(t(cfg, "no_data"))}</p>'
    H = t(cfg, "h")
    rows = []
    for h, share in sp["share"].items():
        w = round((share or 0) * 100)
        rows.append(f'<div class="srow"><span>{h} {e(H)}</span><div class="track"><div class="fill" '
                    f'style="width:{w}%"></div></div><b>{fmt_pct(share)}</b></div>')
    half = (f'<p class="big">{e(t(cfg, "d_half"))}<b>{e(lag_text(sp["half"], cfg))}</b></p>'
            if sp["half"] is not None else "")
    return '<div class="speed">' + "".join(rows) + "</div>" + half


# ── blocks ─────────────────────────────────────────────────────────────────

def kpis(p: dict, cfg: dict) -> str:
    sm, prev = p["summary"], p["prev"]
    cards = [("stories", t(cfg, "stories"), "count", True),
             ("views", t(cfg, "d_views_total"), "views_total", True),
             ("median", t(cfg, "median"), "views_median", True),
             ("unique", t(cfg, "unique_viewers"), "unique_viewers", True),
             ("new", t(cfg, "new_viewers"), "new_viewers", True),
             ("lost", t(cfg, "lost_viewers"), "lost_viewers", False),
             ("reactions", "❤ " + t(cfg, "reactions"), "reactions_total", True),
             ("replies", "💬 " + t(cfg, "replies"), "replies_total", True)]
    if not p["days"]:
        cards = [c for c in cards if c[0] != "lost"]   # "stopped viewing" needs a window before the period
    out = ['<div class="kpis">']
    for _, label, key, good_up in cards:
        cur = sm.get(key)
        delta = ""
        if prev is not None and prev.get("count"):
            before = prev.get(key)
            if before:
                ch = (cur or 0) / before - 1
                cls = "" if abs(ch) < 0.05 else ("up" if (ch > 0) == good_up else "down")
                delta = (f'<div class="d"><span class="{cls}">{"+" if ch > 0 else ""}{round(ch * 100)}%</span> '
                         f'{e(t(cfg, "d_vs_prev"))}</div>')
            elif before == 0 and cur:
                delta = f'<div class="d">0 → {e(fmt_num(cur, cfg))}</div>'
        out.append(f'<div class="kpi"><div class="l">{e(label)}</div><div class="v">{e(fmt_num(cur, cfg))}</div>'
                   f'{delta}</div>')
    return "".join(out) + "</div>"


def gallery(items: list, cfg: dict, tz, thumbs: set) -> str:
    ranked = [i for i in items if i.get("index") is not None and not i.get("young")]
    if len(ranked) < 3:
        return ""
    ranked.sort(key=lambda i: -i["index"])
    out = []
    for it in ranked[:GALLERY]:
        sid = it["story_id"]
        th = f"th-{sid}" if sid in thumbs else ""
        icon = "" if th else MEDIA_ICON.get(it.get("media_kind") or "", "")
        out.append(f'<figure class="sc" data-story="{sid}" tabindex="0"><div class="th {th}">{icon}</div>'
                   f'<figcaption><div class="row"><b>👁 {e(fmt_num(it["views"], cfg))}</b>'
                   f'<span class="badge {index_class(it["index"])}">{fmt_index(it["index"])}</span></div>'
                   f'<div class="dt2">{_date(it["posted_at"], tz)}</div>'
                   + (f'<p class="cp">{e(it["caption"])}</p>' if it.get("caption") else "")
                   + "</figcaption></figure>")
    return (f'<div class="card" style="margin-bottom:16px"><h2>{e(t(cfg, "d_top"))}</h2>'
            f'<p class="note">{e(t(cfg, "d_top_note"))}</p><div class="gallery">{"".join(out)}</div></div>')


def stories_table(items: list, cfg: dict, tz) -> str:
    head = (f'<tr><th class="n" data-sort="num">{e(t(cfg, "id"))}</th><th data-sort="num">{e(t(cfg, "date"))}</th>'
            f'<th class="c">{e(t(cfg, "type"))}</th><th>{e(t(cfg, "caption"))}</th>'
            f'<th class="n" data-sort="num">👁</th><th class="n" data-sort="num">❤</th>'
            f'<th class="n" data-sort="num">💬</th><th class="n" data-sort="num">↪</th>'
            f'<th class="n" data-sort="num">{e(t(cfg, "first_hour"))}</th>'
            f'<th class="n" data-sort="num">{e(t(cfg, "index"))}</th></tr>')
    rows = []
    for it in reversed(items):
        ix, fh = it.get("index"), it.get("first_hour_share")
        young = f' title="{e(t(cfg, "d_young"))}"' if it.get("young") else ""
        rows.append(
            f'<tr data-story="{it["story_id"]}" tabindex="0"><td class="n">{it["story_id"]}</td>'
            f'<td class="nw" data-v="{it["posted_at"]}">{_date(it["posted_at"], tz, "%d.%m.%y %H:%M")}</td>'
            f'<td class="c">{MEDIA_ICON.get(it.get("media_kind") or "", "")}</td>'
            f'<td class="cap"><span>{e(it.get("caption") or "")}</span></td>'
            f'<td class="n" data-v="{it["views"] or 0}">{e(fmt_num(it["views"], cfg))}</td>'
            f'<td class="n">{it.get("reactions") or 0}</td><td class="n">{it.get("replies") or 0}</td>'
            f'<td class="n">{it.get("forwards") or 0}</td>'
            f'<td class="n" data-v="{"" if fh is None else round(fh, 4)}">{fmt_pct(fh)}</td>'
            f'<td class="n {index_class(ix)}" data-v="{"" if ix is None else round(ix, 4)}"{young}>'
            f'{fmt_index(ix)}{" ⏳" if it.get("young") and ix is not None else ""}</td></tr>')
    return (f'<div class="card"><h2>{e(t(cfg, "d_all_stories"))}</h2>'
            f'<p class="hint" style="margin:0 0 8px">{e(t(cfg, "d_click_story"))}</p>'
            f'<div class="tw"><table class="sortable"><thead>{head}</thead><tbody>{"".join(rows)}</tbody>'
            f'</table></div><p class="nojs">{e(t(cfg, "d_nojs"))}</p></div>')


def period_panel(p: dict, cfg: dict, tz, tz_label: str, thumbs: set) -> str:
    items = p["summary"]["items"]
    body = [kpis(p, cfg)]
    if not items:
        body.append(f'<div class="card"><p class="empty">{e(t(cfg, "d_no_stories"))}</p></div>')
        return "".join(body)
    body.append(f'<div class="card" style="margin-bottom:16px"><h2>{e(t(cfg, "d_per_story"))}</h2>'
                f'<p class="note">{e(t(cfg, "d_per_story_note"))}</p>{bars_svg(items, cfg, tz)}</div>')
    body.append(gallery(items, cfg, tz, thumbs))
    body.append(f'<div class="grid g2"><div class="card"><h2>{e(t(cfg, "d_speed"))}</h2>'
                f'<p class="note">{e(t(cfg, "d_speed_note"))}</p>{speed_block(p["speed"], cfg)}</div>'
                f'<div class="card"><h2>{e(t(cfg, "best_hours"))}</h2><p class="note">{e(t(cfg, "d_best_note"))}'
                f'</p>{best_hours(p["hours"], cfg)}</div></div>')
    body.append(f'<div class="card" style="margin-bottom:16px"><h2>{e(t(cfg, "hours_header"))}</h2>'
                f'{heatmap(p["hours"], cfg, tz_label)}</div>')
    body.append(stories_table(items, cfg, tz))
    return "".join(body)


def audience(people: list, cfg: dict, tz) -> str:
    counts = {s: 0 for s in analytics.STATUS_ORDER}
    for p in people:
        counts[p["status"]] += 1
    total = len(people) or 1
    bar = "".join(f'<span style="width:{counts[s] * 100 / total:.2f}%;background:{STATUS_COLOR[s]}" '
                  f'title="{e(t(cfg, s))}: {counts[s]}"></span>' for s in analytics.STATUS_ORDER if counts[s])
    legend = "".join(
        f'<div class="k" data-status-filter="{s}"><i style="background:{STATUS_COLOR[s]}"></i><span>'
        f'<b>{e(t(cfg, s))} · {e(fmt_num(counts[s], cfg))}</b> — {e(t(cfg, "d_status_" + s))}</span></div>'
        for s in analytics.STATUS_ORDER)
    options = "".join(f'<option value="{s}">{e(t(cfg, s))} ({counts[s]})</option>'
                      for s in analytics.STATUS_ORDER if counts[s])
    head = (f'<tr><th data-sort="text">{e(t(cfg, "name"))}</th><th data-sort="text">{e(t(cfg, "nick"))}</th>'
            f'<th>{e(t(cfg, "status"))}</th><th class="n" data-sort="num">{e(t(cfg, "seen"))}</th>'
            f'<th class="n" data-sort="num">{e(t(cfg, "lag"))}</th><th data-sort="num">{e(t(cfg, "last"))}</th>'
            f'<th class="n" data-sort="num">❤</th><th class="n" data-sort="num">💬</th>'
            f'<th class="c">{e(t(cfg, "who"))}</th></tr>')
    rows = []
    for i, p in enumerate(people):
        name, user = full_name(p), p.get("username") or ""
        nick = (f'<a href="https://t.me/{e(user)}" target="_blank" rel="noopener noreferrer">@{e(user)}</a>'
                if user else "—")
        q = f"{name} {user}".lower()
        rows.append(
            f'<tr data-person="{i}" data-status="{p["status"]}" data-q="{e(q)}" tabindex="0">'
            f'<td>{e(name)}</td><td>{nick}</td>'
            f'<td class="nw"><span class="dot" style="background:{STATUS_COLOR[p["status"]]}"></span>'
            f'{e(t(cfg, p["status"]))}</td>'
            f'<td class="n" data-v="{round(p["share"], 4)}">{p["seen_last20"]}/{p["eligible_last20"]}</td>'
            f'<td class="n" data-v="{"" if p["lag"] is None else int(p["lag"])}">{e(lag_text(p["lag"], cfg))}</td>'
            f'<td class="nw" data-v="{p["last"] or ""}">{_date(p["last"], tz)}</td>'
            f'<td class="n">{p["reactions"]}</td><td class="n">{p["replies"]}</td>'
            f'<td class="c">{e(who_cell(p))}</td></tr>')
    return (f'<section class="block"><h2>{e(t(cfg, "people_header"))}</h2><div class="card">'
            f'<div class="sbar">{bar}</div><div class="slegend">{legend}</div>'
            f'<div class="ctl"><input id="people-q" type="search" placeholder="{e(t(cfg, "d_search"))}" '
            f'aria-label="{e(t(cfg, "d_search"))}"><select id="people-st" aria-label="{e(t(cfg, "status"))}">'
            f'<option value="">{e(t(cfg, "d_all_statuses"))}</option>{options}</select>'
            f'<span class="count" id="people-count"></span></div>'
            f'<div class="tw"><table class="sortable"><thead>{head}</thead><tbody id="people-body">{"".join(rows)}'
            f'</tbody></table></div><div class="more"><button class="btn" id="people-more" type="button" hidden>'
            f'{e(t(cfg, "d_show_all"))}</button></div>'
            f'<p class="hint">{e(t(cfg, "d_click_person"))}</p></div></section>')


def channels_block(channels: list, cfg: dict, tz) -> str:
    out = []
    for ch in channels:
        items = ch["stories"]
        title = ch["title"] or (f"@{ch['username']}" if ch["username"] else str(ch["peer_id"]))
        rows = "".join(
            f'<tr><td class="n">{s["story_id"]}</td><td class="nw" data-v="{s["posted_at"] or ""}">'
            f'{_date(s["posted_at"], tz)}</td><td class="c">{MEDIA_ICON.get(s.get("media_kind") or "", "")}</td>'
            f'<td class="cap"><span>{e(s.get("caption") or "")}</span></td>'
            f'<td class="n">{e(fmt_num(s.get("views") or 0, cfg))}</td><td class="n">{s.get("reactions") or 0}</td>'
            f'<td class="n">{s.get("forwards") or 0}</td></tr>' for s in reversed(items))
        views = [s.get("views") or 0 for s in items]
        line = (f'{t(cfg, "stories")}: {len(items)} · 👁 {fmt_num(sum(views), cfg)} · {t(cfg, "median").lower()} '
                f'{fmt_num(analytics.median(views), cfg)}')
        out.append(
            f'<div class="card" style="margin-bottom:16px"><h2>{e(title)}</h2><p class="note">{e(line)}. '
            f'{e(t(cfg, "channel_viewers_hidden"))}.</p>'
            + (f'<div class="tw"><table class="sortable"><thead><tr><th class="n" data-sort="num">{e(t(cfg, "id"))}'
               f'</th><th data-sort="num">{e(t(cfg, "date"))}</th><th class="c">{e(t(cfg, "type"))}</th>'
               f'<th>{e(t(cfg, "caption"))}</th><th class="n" data-sort="num">👁</th>'
               f'<th class="n" data-sort="num">❤</th><th class="n" data-sort="num">↪</th></tr></thead>'
               f'<tbody>{rows}</tbody></table></div>' if items else f'<p class="empty">{e(t(cfg, "no_data"))}</p>')
            + "</div>")
    return (f'<section class="block"><h2>{e(t(cfg, "channel_header"))}</h2>{"".join(out)}</section>'
            if out else "")


def autoresponder_block(data: dict, cfg: dict) -> str:
    on = data["auto_enabled"]
    state = [f'<span class="chip"><b class="{"on" if on else "off"}">●</b> '
             f'{e(t(cfg, "d_auto_on" if on else "d_auto_off"))}</span>']
    if data["kill_switch"]:
        state.append(f'<span class="chip"><b class="down">■</b> {e(t(cfg, "d_kill"))}</span>')
    if not data["rules"]:
        body = f'<p class="empty">{e(t(cfg, "d_no_rules"))}</p>'
    else:
        head = (f'<tr><th class="n">{e(t(cfg, "id"))}</th><th>{e(t(cfg, "d_rule"))}</th><th>{e(t(cfg, "status"))}</th>'
                f'<th>{e(t(cfg, "d_action"))}</th><th>{e(t(cfg, "d_scope"))}</th>'
                f'<th class="n">{e(t(cfg, "d_sent"))}</th><th class="n">{e(t(cfg, "d_replied"))}</th>'
                f'<th class="n">{e(t(cfg, "d_queued"))}</th><th class="n">{e(t(cfg, "d_skipped"))}</th>'
                f'<th class="n">{e(t(cfg, "d_failed"))}</th><th class="n">{e(t(cfg, "d_test"))}</th></tr>')
        rows = []
        for r in data["rules"]:
            c, spec = r["counts"], r["spec"]
            action = (spec.get("action") or {}).get("type", "")
            scope = spec.get("scope") or cfg.get("autoresponder", {}).get("default_scope", "contacts")
            rows.append(f'<tr><td class="n">{r["id"]}</td><td>{e(r["name"])}</td>'
                        f'<td>{e(t(cfg, "d_r_" + (r["status"] or "draft")))}</td>'
                        f'<td>{e(t(cfg, "d_a_" + action) if action else "")}</td>'
                        f'<td>{e(scope if action == "dm" else "—")}</td>'
                        f'<td class="n">{c.get("sent", 0)}</td><td class="n">{c.get("replied", 0)}</td>'
                        f'<td class="n">{c.get("queued", 0)}</td><td class="n">{c.get("skipped", 0)}</td>'
                        f'<td class="n">{c.get("failed", 0)}</td><td class="n">{c.get("shadow", 0)}</td></tr>')
        body = f'<div class="tw"><table><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'
    return (f'<section class="block"><h2>{e(t(cfg, "d_auto"))}</h2><div class="card">'
            f'<div class="state">{"".join(state)}</div>{body}</div></section>')


# ── page ───────────────────────────────────────────────────────────────────

def _thumbs_css(con, peer_id: int, ids: set) -> tuple[str, set]:
    css, have = [], set()
    folder = config.data_dir() / "thumbs"
    for sid in sorted(ids):
        row = con.execute("SELECT thumb FROM stories WHERE peer_id=? AND story_id=?", (peer_id, sid)).fetchone()
        name = row["thumb"] if row else None
        if not name or "/" in name or "\\" in name:
            continue
        path = folder / name
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not raw or len(raw) > THUMB_MAX_BYTES or not raw.startswith(b"\xff\xd8"):
            continue   # JPEG only: the CSS below says image/jpeg
        css.append(f'.th-{sid}{{background-image:url("data:image/jpeg;base64,{base64.b64encode(raw).decode()}")}}')
        have.add(sid)
    return "\n".join(css), have


def _client_data(data: dict, cfg: dict, thumbs: set) -> dict:
    """What the script needs for viewer lists: people, stories, views — compact."""
    pidx, people = {}, []
    for p in data["people"]:
        pidx[p["user_id"]] = len(people)
        people.append([full_name(p), p.get("username") or "", who_cell(p), p["status"], p.get("first") or 0])
    stories = {}
    for p in data["periods"]:
        if p["key"] != "all":
            continue
        for it in p["summary"]["items"]:
            stories[str(it["story_id"])] = {
                "t": it["posted_at"], "k": it.get("media_kind") or "", "c": it.get("caption") or "",
                "v": it["views"] or 0, "r": it.get("reactions") or 0, "rp": it.get("replies") or 0,
                "f": it.get("forwards") or 0, "l": it.get("viewers_listed") or 0, "a": it.get("list_available"),
                "ix": fmt_index(it.get("index")) or None,
                "th": f"th-{it['story_id']}" if it["story_id"] in thumbs else ""}
    reacts, ridx, views = [], {}, {}
    for r in data["views"]:
        if str(r["story_id"]) not in stories or r["user_id"] not in pidx:
            continue
        row = [pidx[r["user_id"]], max(0, (r["first_viewed_at"] or 0) - (r["posted_at"] or 0))]
        rc = reaction_cell(r["reaction"])
        if rc:
            if rc not in ridx:
                ridx[rc] = len(reacts) + 1
                reacts.append(rc)
            row.append(ridx[rc])
        views.setdefault(str(r["story_id"]), []).append(row)
    tz = data["tz"]
    off = datetime.fromtimestamp(data["now"], tz).utcoffset()
    keys = ("n", "time", "after", "name", "nick", "reaction", "who", "story", "index", "date", "caption", "new",
            "no_data", "h", "m", "d")
    strings = {k: t(cfg, k) for k in keys}
    strings.update({"listed": t(cfg, "d_listed"), "no_list": t(cfg, "d_no_list"), "close": t(cfg, "d_close"),
                    "seen_stories": t(cfg, "d_seen_stories"),
                    "status": {s: t(cfg, s) for s in analytics.STATUS_ORDER}})
    return {"t": strings, "tz": getattr(tz, "key", None) or ("UTC" if off is not None and not off else None),
            "off": int(off.total_seconds() // 60) if off is not None else 0,
            "sep": " " if cfg.get("locale") == "ru" else ",",
            "icons": MEDIA_ICON, "people": people, "stories": stories, "views": views, "reacts": reacts}


# inside <script type="application/json"> a "<" could close the element: these three go as JSON escapes
_SCRIPT_SAFE = str.maketrans({c: f"\\u{ord(c):04x}" for c in "<>&"})


def _json_for_script(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).translate(_SCRIPT_SAFE)


def render(con, cfg: dict, data: dict, *, period: str | None = None, thumbs: bool = True) -> str:
    tz = data["tz"]
    tz_label = (cfg.get("timezone") or "").strip() or datetime.fromtimestamp(data["now"], tz).strftime("UTC%z")
    selected = default_period(data, period)
    want = set()
    if thumbs:
        for p in data["periods"]:
            ranked = [i for i in p["summary"]["items"] if i.get("index") is not None and not i.get("young")]
            ranked.sort(key=lambda i: -i["index"])
            want.update(i["story_id"] for i in ranked[:GALLERY])
    thumbs_css, have = _thumbs_css(con, data["peer_id"], want) if want else ("", set())

    owner = data["owner"]
    who = owner.get("title") or ""
    if owner.get("username"):
        who += f" · @{owner['username']}"
    all_sm = next(p["summary"] for p in data["periods"] if p["key"] == "all")
    chips = [f'<span class="chip">{e(t(cfg, "stories"))} <b>{e(fmt_num(all_sm["count"], cfg))}</b></span>',
             f'<span class="chip">{e(t(cfg, "d_viewers"))} <b>{e(fmt_num(len(data["people"]), cfg))}</b></span>',
             f'<span class="chip">{e(t(cfg, "active_30d"))} <b>{e(fmt_num(all_sm["active_30d"], cfg))}</b></span>']
    sub = f'{e(t(cfg, "d_made"))} {_date(data["now"], tz, "%d.%m.%Y %H:%M")}'
    if data["last_poll"]:
        sub += f' · {e(t(cfg, "d_data_at"))} {_date(data["last_poll"], tz, "%d.%m.%Y %H:%M")}'
    body = [f'<header class="top"><div><h1>{e(t(cfg, "d_title"))}</h1><div class="sub">{e(who)}'
            f'{" · " if who else ""}{sub}</div></div><div class="chips">{"".join(chips)}</div></header>',
            f'<p class="private">🔒 {e(t(cfg, "d_private"))}</p>']
    radios = "".join(f'<input class="tab-input" type="radio" name="period" id="p-{p["key"]}"'
                     f'{" checked" if p["key"] == selected else ""}>' for p in data["periods"])
    labels = "".join(f'<label for="p-{p["key"]}">{e(t(cfg, "d_p" + p["key"]))}</label>' for p in data["periods"])
    panels = "".join(f'<section class="panel" id="panel-{p["key"]}">{period_panel(p, cfg, tz, tz_label, have)}'
                     f'</section>' for p in data["periods"])
    body.append(f'{radios}<nav class="tabs">{labels}</nav><div class="panels">{panels}</div>')
    body.append(f'<section class="block"><h2>{e(t(cfg, "d_trend"))}</h2><div class="card">'
                f'<p class="note">{e(t(cfg, "d_trend_note"))}</p>{trend_svg(data["monthly"], cfg)}</div></section>')
    body.append(audience(data["people"], cfg, tz))
    body.append(channels_block(data["channels"], cfg, tz))
    body.append(autoresponder_block(data, cfg))
    body.append(f'<footer>{e(t(cfg, "d_footer").replace("{v}", __version__))}</footer>')

    tab_css = "\n".join(
        f'#p-{p["key"]}:checked~.tabs label[for=p-{p["key"]}]{{background:var(--card);color:var(--ink);'
        f'box-shadow:var(--shadow)}}#p-{p["key"]}:checked~.panels #panel-{p["key"]}{{display:block}}'
        for p in data["periods"])
    values = {"TAB_CSS": tab_css, "THUMBS": thumbs_css, "LANG": e(cfg.get("locale") or "en"),
              "TITLE": e(t(cfg, "d_title") + (f" · {owner['title']}" if owner.get("title") else "")),
              "DATA": _json_for_script(_client_data(data, cfg, have)),
              "BODY": "\n".join(b for b in body if b)}
    # one pass: a name or caption that happens to contain "{{DATA}}" stays text, not a placeholder
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values.get(m.group(1), m.group(0)),
                  TEMPLATE.read_text(encoding="utf-8"))


def default_path() -> Path:
    return config.data_dir() / "dashboard" / "dashboard.html"


def build(cfg: dict, out: str | Path | None = None, *, period: str | None = None, thumbs: bool = True,
          con=None) -> Path:
    con = con or db.connect(create=False)
    page = render(con, cfg, collect(con, cfg), period=period, thumbs=thumbs)
    path = Path(out) if out else default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dashboard.", suffix=".html", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(page)
        os.chmod(tmp, 0o600)   # viewers' names: owner-only, like the database
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path
