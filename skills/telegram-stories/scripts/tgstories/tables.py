"""Tables: Markdown (Telegram Rich Messages render pipe tables natively), CSV, JSON, plain text.

Readability rules: at most ~8 narrow columns (phones), short headers, names cut
to a sane length, @username always clickable, long tables cut with a CSV export.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import analytics, config, db
from .i18n import t

WHO_ICON = {"mutual": "👥", "contact": "📇", "other": "·"}
MESSAGE_BUDGET = 30000   # characters of one table; Telegram rich messages hold 32 768
MEDIA_ICON = {"photo": "📷", "video": "🎬", "document": "📄"}
_MD_ESCAPE = re.compile(r"([\\`*_\[\]|<>~])")


def md_escape(text) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ")
    return _MD_ESCAPE.sub(r"\\\1", text)


def cut(text, n: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


_URL = re.compile(r"(https?://|www\.|t\.me/)\S+", re.IGNORECASE)


def caption_cut(text, n: int) -> str:
    """A caption shortened for a table cell. Links become 🔗 and the cut never lands inside a word:
    Telegram auto-links what looks like a URL or @username, and half of one would link somewhere else."""
    text = _URL.sub("🔗", (text or "").replace("\n", " "))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= n:
        return text
    head = text[: n - 1]
    if " " in head and not text[n - 1].isspace():
        head = head[: head.rfind(" ")]
    return head.rstrip(" ,.;:—-") + "…"


@dataclass
class Col:
    key: str
    title: str
    align: str = "left"     # left | right | center


@dataclass
class Table:
    cols: list
    rows: list = field(default_factory=list)       # dicts: key -> markdown cell
    plain: list = field(default_factory=list)      # dicts: key -> plain value (csv/json/text)
    title: str = ""
    notes: list = field(default_factory=list)      # markdown lines above the table
    footer: list = field(default_factory=list)     # markdown lines below the table

    def to_md(self, max_rows: int | None = None) -> str:
        out = []
        if self.title:
            out.append(self.title)
        out.extend(self.notes)
        if out:
            out.append("")
        head = "| " + " | ".join(md_escape(c.title) if c.title else " " for c in self.cols) + " |"
        sep = "|" + "|".join({"right": "---:", "center": ":---:"}.get(c.align, "---") for c in self.cols) + "|"
        out += [head, sep]
        rows = self.rows if max_rows is None else self.rows[:max_rows]
        for r in rows:
            out.append("| " + " | ".join(str(r.get(c.key, "") if r.get(c.key, "") != "" else " ")
                                         for c in self.cols) + " |")
        if not self.rows:
            out.append("| " + " | ".join(" " for _ in self.cols) + " |")
        footer = list(self.footer)
        if footer:
            out.append("")
            out.extend(footer)
        return "\n".join(out)

    def to_csv(self, delimiter: str = ",") -> str:
        buf = io.StringIO()
        keys = list(self.plain[0].keys()) if self.plain else [c.key for c in self.cols]
        w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore", lineterminator="\n", delimiter=delimiter)
        w.writeheader()
        comma = delimiter == ";"   # spreadsheets that split on ";" read decimals with a comma (1,5 not 1.5)
        for r in self.plain:
            if comma:
                r = {k: (str(v).replace(".", ",") if isinstance(v, float) else v) for k, v in r.items()}
            w.writerow(r)
        return buf.getvalue()

    def to_json(self) -> str:
        return json.dumps({"title": _strip_md(self.title), "notes": [_strip_md(n) for n in self.notes],
                           "rows": self.plain, "footer": [_strip_md(f) for f in self.footer]},
                          ensure_ascii=False, indent=1, default=str)

    def to_text(self, max_rows: int | None = None) -> str:
        rows = self.rows if max_rows is None else self.rows[:max_rows]
        cells = [[_strip_md(c.title) for c in self.cols]]
        for r in rows:
            cells.append([_strip_md(str(r.get(c.key, ""))) for c in self.cols])
        widths = [max(len(row[i]) for row in cells) for i in range(len(self.cols))]
        lines = [_strip_md(self.title)] if self.title else []
        lines += [_strip_md(n) for n in self.notes]
        for row in cells:
            lines.append("  ".join(v.rjust(widths[i]) if self.cols[i].align == "right" else v.ljust(widths[i])
                                   for i, v in enumerate(row)).rstrip())
        lines += [_strip_md(f) for f in self.footer]
        return "\n".join(lines)


def _strip_md(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text or "")
    text = text.replace("**", "").replace("__", "")
    return re.sub(r"\\(.)", r"\1", text)


# ── cells ──────────────────────────────────────────────────────────────────

def full_name(p: dict) -> str:
    name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x).strip()
    if p.get("deleted") or p.get("p_deleted"):
        return name or "Deleted account"
    return name or (f"@{p['username']}" if p.get("username") else f"id{p.get('user_id')}")


def name_cell(p: dict, cfg: dict, limit: int = 26) -> str:
    """Plain name by default. Verified in Telegram: a bot's rich message turns `tg://user?id=` links into
    plain text, so people without a username cannot get a working link from a bot; `tg_id` is for
    userbot contexts only."""
    name = md_escape(cut(full_name(p), limit))
    mode = cfg.get("tables", {}).get("name_links", "never")
    uid, user = p.get("user_id"), p.get("username")
    if user and mode in ("always", "username"):
        return f"[{name}](https://t.me/{user})"
    if not user and uid and mode == "tg_id":
        return f"[{name}](tg://user?id={uid})"
    return name


def nick_cell(p: dict, cfg: dict) -> str:
    user = p.get("username")
    if not user:
        return "—"
    mode = cfg.get("tables", {}).get("nick_links", "link")
    if mode == "mention":
        return "@" + md_escape(user)
    return f"[@{md_escape(user)}](https://t.me/{user})"


def who_cell(p: dict) -> str:
    kind = "mutual" if p.get("mutual") else ("contact" if p.get("is_contact") else "other")
    return ("⭐" if p.get("close_friend") else "") + WHO_ICON[kind]


def who_plain(p: dict) -> str:
    return "mutual" if p.get("mutual") else ("contact" if p.get("is_contact") else "other")


def reaction_cell(r) -> str:
    if not r:
        return ""
    if r.startswith("custom:"):
        return "✨"
    if r == "paid":
        return "⭐"
    return r


def fmt_time(ts, tz, ref_ts=None) -> str:
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts, tz)
    if ref_ts:
        ref = datetime.fromtimestamp(ref_ts, tz)
        if ref.date() == dt.date():
            return dt.strftime("%H:%M")
    return dt.strftime("%d.%m %H:%M")


def fmt_date(ts, tz, with_year=False) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m.%Y" if with_year else "%d.%m")


def fmt_lag(seconds, cfg) -> str:
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    H, M, D = t(cfg, "h"), t(cfg, "m"), t(cfg, "d")
    if d:
        return f"+{d}{D}{h}{H}" if h else f"+{d}{D}"
    if h:
        return f"+{h}{H}{m:02d}{M}" if m else f"+{h}{H}"
    return f"+{m}{M}"


def fmt_index(ix) -> str:
    if ix is None:
        return ""
    pct = round((ix - 1) * 100)
    return f"{pct:+d}%"


def iso(ts, tz) -> str:
    return datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds") if ts else ""


# ── peers ──────────────────────────────────────────────────────────────────

def resolve_peer(con, ref: str | None) -> tuple[int, str]:
    if not ref or ref in ("me", "self"):
        oid = db.owner_id(con)
        if not oid:
            raise SystemExit("owner unknown — run `stories.py login` first")
        return oid, "self"
    ref_l = ref.lstrip("@").lower()
    for row in con.execute("SELECT peer_id, kind, username, title FROM peers"):
        if (row["username"] or "").lower() == ref_l or str(row["peer_id"]) == ref:
            return int(row["peer_id"]), row["kind"]
    raise SystemExit(f"{ref} is not tracked — add it to `channels` in the config and run backfill --channel {ref}")


# ── tables ─────────────────────────────────────────────────────────────────

def story_table(con, cfg, peer_id: int, ref) -> Table:
    tz = config.get_tz(cfg)
    sid = analytics.resolve_story(con, peer_id, ref)
    m = analytics.story(con, peer_id, sid, cfg.get("pulse", {}).get("baseline_stories", 20))
    s = m["story"]
    icon = MEDIA_ICON.get(s.get("media_kind") or "", "")
    kind = t(cfg, s.get("media_kind") or "photo") if s.get("media_kind") else ""
    title = (f"**{t(cfg, 'story_header')} {sid}** · {fmt_date(s['posted_at'], tz, True)} "
             f"{fmt_time(s['posted_at'], tz, s['posted_at'])} · {icon} {kind}").strip()
    notes = []
    if s.get("caption"):
        notes.append("_" + md_escape(caption_cut(s["caption"], 120)) + "_")
    line = f"👁 {m['views']} · ❤ {m['reactions']} · 💬 {m['replies']} · ↪ {m['forwards']}"
    if m["index"] is not None:
        line += f" · {t(cfg, 'index')} {fmt_index(m['index'])}" + (" ⏳" if m.get("young") else "")
    notes.append(line)
    if m["listed"]:
        at = m["at"]
        H = t(cfg, "h")
        notes.append(f"1{H}: {at[1]} · 6{H}: {at[6]} · 24{H}: {at[24]} · 48{H}: {at[48]}"
                     + (f" · {t(cfg, 'new_viewers').lower()}: {m['new_viewers']}" if m["new_viewers"] else ""))
        c = m["composition"]
        notes.append(f"👥 {c['mutual']} · 📇 {c['contact']} · · {c['other']}"
                     + (f" · ⭐ {c['close_friend']}" if c["close_friend"] else ""))
    elif m["list_available"] == 0:
        notes.append("_" + md_escape(t(cfg, "viewers_hidden")) + "_")
    cols = [Col("n", t(cfg, "n"), "right"), Col("time", t(cfg, "time")), Col("after", t(cfg, "after"), "right"),
            Col("name", t(cfg, "name")), Col("nick", t(cfg, "nick")), Col("reaction", t(cfg, "reaction"), "center"),
            Col("who", t(cfg, "who"), "center")]
    tb = Table(cols=cols, title=title, notes=notes)
    for i, v in enumerate(m["viewers"], 1):
        lag = (v["first_viewed_at"] or 0) - (s["posted_at"] or 0)
        tb.rows.append({"n": i, "time": fmt_time(v["first_viewed_at"], tz, s["posted_at"]),
                        "after": fmt_lag(lag, cfg), "name": name_cell(v, cfg), "nick": nick_cell(v, cfg),
                        "reaction": reaction_cell(v.get("reaction")), "who": who_cell(v)})
        tb.plain.append({"n": i, "story_id": sid, "user_id": v["user_id"], "first_name": v.get("first_name") or "",
                         "last_name": v.get("last_name") or "", "username": v.get("username") or "",
                         "viewed_at": iso(v["first_viewed_at"], tz), "last_viewed_at": iso(v["viewed_at"], tz),
                         "after_min": lag // 60, "reaction": v.get("reaction") or "", "who": who_plain(v),
                         "close_friend": int(bool(v.get("close_friend")))})
    if tb.rows:
        tb.footer.append(f"_👥 {t(cfg, 'mutual')} · 📇 {t(cfg, 'contact')} · ⭐ {t(cfg, 'close_friend')} · "
                         f"· {t(cfg, 'other')}_")
    return tb


def summary_table(con, cfg, peer_id: int, start: int, end: int) -> Table:
    tz = config.get_tz(cfg)
    sm = analytics.summary(con, peer_id, start, end)
    title = (f"**{t(cfg, 'summary_header')}** · {fmt_date(start, tz, True) if start else '…'} — "
             f"{fmt_date(end - 1, tz, True)}")
    med = sm["views_median"]
    notes = [f"{t(cfg, 'stories')}: {sm['count']} · 👁 {sm['views_total']} · {t(cfg, 'median').lower()} "
             f"{round(med) if med is not None else '—'} · ❤ {sm['reactions_total']} · 💬 {sm['replies_total']}",
             f"{t(cfg, 'unique_viewers')}: {sm['unique_viewers']} · {t(cfg, 'new_viewers')}: {sm['new_viewers']} · "
             f"{t(cfg, 'lost_viewers')}: {sm['lost_viewers']} · {t(cfg, 'active_30d')}: {sm['active_30d']}"]
    cols = [Col("id", t(cfg, "id"), "right"), Col("date", t(cfg, "date")), Col("story", t(cfg, "story")),
            Col("views", "👁", "right"), Col("reactions", "❤", "right"), Col("replies", "💬", "right"),
            Col("first_hour", t(cfg, "first_hour"), "right"), Col("index", t(cfg, "index"), "right")]
    tb = Table(cols=cols, title=title, notes=notes)
    if any(s.get("young") for s in sm["items"]):
        tb.footer.append("_⏳ " + md_escape(t(cfg, "young_note")) + "_")
    for s in sm["items"]:
        icon = MEDIA_ICON.get(s.get("media_kind") or "", "")
        caption = md_escape(caption_cut(s.get("caption") or "", 28))
        fh = s.get("first_hour_share")
        tb.rows.append({"id": s["story_id"], "date": fmt_date(s["posted_at"], tz), "story": f"{icon} {caption}".strip(),
                        "views": s["views"] if s["views"] is not None else "", "reactions": s.get("reactions") or 0,
                        "replies": s["replies"], "first_hour": f"{round(fh * 100)}%" if fh is not None else "",
                        "index": fmt_index(s["index"]) + (" ⏳" if s.get("young") and s["index"] else "")})
        tb.plain.append({"story_id": s["story_id"], "posted_at": iso(s["posted_at"], tz),
                         "media": s.get("media_kind") or "", "caption": s.get("caption") or "",
                         "views": s["views"], "viewers_listed": s.get("viewers_listed") or 0,
                         "reactions": s.get("reactions") or 0, "replies": s["replies"],
                         "forwards": s.get("forwards") or 0, "first_hour_views": s.get("first_hour"),
                         "index_vs_median": round(s["index"], 3) if s["index"] else None})
    return tb


def people_table(con, cfg, peer_id: int, status: str | None = None, sort: str | None = None) -> Table:
    tz = config.get_tz(cfg)
    ppl = analytics.people(con, peer_id)
    if status:
        ppl = [p for p in ppl if p["status"] == status]
    if sort in ("last", "share", "seen_total", "reactions", "replies"):
        ppl.sort(key=lambda p: -(p.get(sort) or 0))
    counts: dict = {}
    for p in analytics.people(con, peer_id) if status else ppl:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    title = f"**{t(cfg, 'people_header')}**" + (f" · {t(cfg, status)}" if status else "")
    notes = [" · ".join(f"{t(cfg, k)} {counts[k]}" for k in analytics.STATUS_ORDER if counts.get(k))]
    cols = [Col("name", t(cfg, "name")), Col("nick", t(cfg, "nick")), Col("status", t(cfg, "status")),
            Col("seen", t(cfg, "seen"), "right"), Col("lag", t(cfg, "lag"), "right"),
            Col("last", t(cfg, "last")), Col("who", t(cfg, "who"), "center")]
    tb = Table(cols=cols, title=title, notes=notes)
    for p in ppl:
        tb.rows.append({"name": name_cell(p, cfg), "nick": nick_cell(p, cfg), "status": t(cfg, p["status"]),
                        "seen": f"{p['seen_last20']}/{p['eligible_last20']}", "lag": fmt_lag(p["lag"], cfg),
                        "last": fmt_date(p["last"], tz), "who": who_cell(p)})
        tb.plain.append({"user_id": p["user_id"], "first_name": p.get("first_name") or "",
                         "last_name": p.get("last_name") or "", "username": p.get("username") or "",
                         "status": p["status"], "seen_total": p["seen_total"], "seen_last20": p["seen_last20"],
                         "eligible_last20": p["eligible_last20"], "share": round(p["share"], 3),
                         "typical_lag_min": round(p["lag"] / 60) if p["lag"] is not None else None,
                         "first_view": iso(p["first"], tz), "last_view": iso(p["last"], tz),
                         "reactions": p["reactions"], "replies": p["replies"], "who": who_plain(p),
                         "close_friend": int(bool(p.get("close_friend")))})
    return tb


def hours_tables(con, cfg, peer_id: int, start: int, end: int) -> list:
    tz = config.get_tz(cfg)
    h = analytics.hours(con, peer_id, start, end, tz)
    total = h["total"] or 1
    peak = max(h["by_hour"]) or 1
    th = Table(cols=[Col("hour", t(cfg, "hour")), Col("views", "👁", "right"), Col("share", "%", "right"),
                     Col("bar", "")], title=f"**{t(cfg, 'hours_header')}**")
    for hour, n in enumerate(h["by_hour"]):
        th.rows.append({"hour": f"{hour:02d}:00", "views": n, "share": f"{round(n * 100 / total)}%",
                        "bar": "▇" * max(0, round(n * 12 / peak))})
        th.plain.append({"hour": hour, "views": n, "share": round(n / total, 4)})
    if h["best_post_hours"]:
        th.footer.append(f"{t(cfg, 'best_hours')}: " + ", ".join(
            f"{hh:02d}:00 (👁 {round(med)}, n={n})" for hh, med, n in h["best_post_hours"]))
    peak_d = max(h["by_day"]) or 1
    td = Table(cols=[Col("day", t(cfg, "weekday")), Col("views", "👁", "right"), Col("share", "%", "right"),
                     Col("bar", "")], title=f"**{t(cfg, 'days_header')}**")
    for i, n in enumerate(h["by_day"]):
        td.rows.append({"day": t(cfg, "weekdays")[i], "views": n, "share": f"{round(n * 100 / total)}%",
                        "bar": "▇" * max(0, round(n * 12 / peak_d))})
        td.plain.append({"weekday": i, "views": n, "share": round(n / total, 4)})
    return [th, td]


def compare_table(con, cfg, peer_id: int, refs: list) -> Table:
    tz = config.get_tz(cfg)
    ms = [analytics.story(con, peer_id, analytics.resolve_story(con, peer_id, r)) for r in refs[:5]]
    cols = [Col("metric", t(cfg, "metric"))] + [Col(f"s{i}", str(m["story"]["story_id"]), "right")
                                                for i, m in enumerate(ms)]
    tb = Table(cols=cols, title=f"**{t(cfg, 'compare_header')}**")
    H = t(cfg, "h")
    lines = [
        (t(cfg, "date"), lambda m: fmt_date(m["story"]["posted_at"], tz)),
        (t(cfg, "type"), lambda m: MEDIA_ICON.get(m["story"].get("media_kind") or "", "")),
        ("👁", lambda m: m["views"]),
        (f"1{H}", lambda m: m["at"][1] if m["listed"] else ""),
        (f"6{H}", lambda m: m["at"][6] if m["listed"] else ""),
        (f"24{H}", lambda m: m["at"][24] if m["listed"] else ""),
        ("❤", lambda m: m["reactions"]),
        ("💬", lambda m: m["replies"]),
        (t(cfg, "new_viewers"), lambda m: m["new_viewers"]),
        (t(cfg, "index"), lambda m: fmt_index(m["index"])),
    ]
    for label, fn in lines:
        tb.rows.append({"metric": label, **{f"s{i}": fn(m) for i, m in enumerate(ms)}})
        tb.plain.append({"metric": _strip_md(label), **{str(m["story"]["story_id"]): fn(m) for m in ms}})
    return tb


def channel_table(con, cfg, peer_id: int, start: int, end: int) -> Table:
    tz = config.get_tz(cfg)
    rows = analytics.channel(con, peer_id, start, end)
    peer = con.execute("SELECT title, username FROM peers WHERE peer_id=?", (peer_id,)).fetchone()
    title = f"**{t(cfg, 'channel_header')}** · {md_escape(peer['title'] if peer else peer_id)}"
    cols = [Col("id", t(cfg, "id"), "right"), Col("date", t(cfg, "date")), Col("story", t(cfg, "story")),
            Col("views", "👁", "right"), Col("reactions", "❤", "right"), Col("forwards", "↪", "right")]
    tb = Table(cols=cols, title=title, notes=["_" + md_escape(t(cfg, "channel_viewers_hidden")) + "_"])
    for s in rows:
        icon = MEDIA_ICON.get(s.get("media_kind") or "", "")
        tb.rows.append({"id": s["story_id"], "date": fmt_date(s["posted_at"], tz, True),
                        "story": f"{icon} {md_escape(caption_cut(s.get('caption') or '', 28))}".strip(),
                        "views": s.get("views") or 0, "reactions": s.get("reactions") or 0,
                        "forwards": s.get("forwards") or 0})
        tb.plain.append({"story_id": s["story_id"], "posted_at": iso(s["posted_at"], tz),
                         "media": s.get("media_kind") or "", "caption": s.get("caption") or "",
                         "views": s.get("views") or 0, "reactions": s.get("reactions") or 0,
                         "forwards": s.get("forwards") or 0, "named_reactions": s["named_reactions"],
                         "public_forwards": s["public_forwards"]})
    return tb


# ── CLI glue ───────────────────────────────────────────────────────────────

def _emit(tables: list, fmt: str, cfg: dict, name: str) -> str:
    max_rows = int(cfg.get("tables", {}).get("max_rows", 150))
    if fmt == "csv":
        return "\n".join(tb.to_csv(_delim(cfg)) for tb in tables)
    if fmt == "json":
        return "[" + ",".join(tb.to_json() for tb in tables) + "]" if len(tables) > 1 else tables[0].to_json()
    parts = []
    for tb in tables:
        render = (lambda n=None: tb.to_md(n)) if fmt == "md" else (lambda n=None: tb.to_text(n))
        # rows AND characters: Telegram's rich message holds 32 768 characters, long usernames can blow it
        # with fewer than max_rows rows — then cut further (a little headroom for the agent's own words)
        limit = min(max_rows, len(tb.rows))
        body = render(limit if limit < len(tb.rows) else None)
        while len(body) > MESSAGE_BUDGET and limit > 10:
            limit = max(10, int(limit * 0.8))
            body = render(limit)
        if limit < len(tb.rows):
            path = _write_export(tb, name, cfg)
            tb.footer.append(f"_{len(tb.rows) - limit} {md_escape(t(cfg, 'more_rows'))}_")
            tb.footer.append(f"CSV: {path}")
            body = render(limit)
        parts.append(body)
    return "\n\n".join(parts)


def _delim(cfg: dict) -> str:
    d = str(cfg.get("tables", {}).get("csv_delimiter", ",") or ",")
    return d[0] if d.strip() else ","


def _write_export(tb: Table, name: str, cfg: dict | None = None) -> Path:
    d = config.data_dir() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    # BOM: Excel opens UTF-8 CSV correctly only with it (Cyrillic, emoji)
    path.write_text(tb.to_csv(_delim(cfg or {})), encoding="utf-8-sig")
    return path


def render_cli(cfg: dict, args) -> str:
    con = db.connect(create=False)
    tz = config.get_tz(cfg)
    peer_id, kind = resolve_peer(con, getattr(args, "peer", None))
    if args.kind == "channel" or kind == "channel":
        if kind != "channel":
            raise SystemExit("`table channel` needs --peer @channel")
        start, end = analytics.parse_period(args.period or "all", args.date_from, args.date_to, tz)
        return _emit([channel_table(con, cfg, peer_id, start, end)], args.format, cfg, "channel")
    if args.kind == "story":
        ref = args.refs[0] if args.refs else "last"
        return _emit([story_table(con, cfg, peer_id, ref)], args.format, cfg, f"story-{ref}")
    if args.kind == "summary":
        start, end = analytics.parse_period(args.period or "30d", args.date_from, args.date_to, tz)
        return _emit([summary_table(con, cfg, peer_id, start, end)], args.format, cfg, "summary")
    if args.kind == "people":
        tb = people_table(con, cfg, peer_id, args.status, args.sort)
        if args.limit:
            tb.rows, tb.plain = tb.rows[: args.limit], tb.plain[: args.limit]
        return _emit([tb], args.format, cfg, "people")
    if args.kind == "hours":
        start, end = analytics.parse_period(args.period or "90d", args.date_from, args.date_to, tz)
        return _emit(hours_tables(con, cfg, peer_id, start, end), args.format, cfg, "hours")
    if args.kind == "compare":
        if len(args.refs) < 2:
            raise SystemExit("compare needs at least two story references")
        return _emit([compare_table(con, cfg, peer_id, args.refs)], args.format, cfg, "compare")
    raise SystemExit(f"unknown table {args.kind}")


def export_csv(cfg: dict, what: str, period: str = "all") -> Path:
    con = db.connect(create=False)
    tz = config.get_tz(cfg)
    peer_id = db.owner_id(con)
    start, end = analytics.parse_period(period, None, None, tz)
    if what == "people":
        tb = people_table(con, cfg, peer_id)
    elif what == "stories":
        tb = summary_table(con, cfg, peer_id, start, end)
    else:
        tb = Table(cols=[])
        for r in con.execute(
                """SELECT v.story_id, v.user_id, p.first_name, p.last_name, p.username, v.first_viewed_at,
                          v.viewed_at, v.reaction, s.posted_at, p.mutual, p.is_contact, p.close_friend
                   FROM views v JOIN stories s ON s.peer_id=v.peer_id AND s.story_id=v.story_id
                   LEFT JOIN people p ON p.user_id=v.user_id
                   WHERE v.peer_id=? AND s.posted_at>=? AND s.posted_at<?
                   ORDER BY s.posted_at, v.first_viewed_at""", (peer_id, start, end)):
            r = dict(r)
            tb.plain.append({"story_id": r["story_id"], "story_posted_at": iso(r["posted_at"], tz),
                             "user_id": r["user_id"], "first_name": r["first_name"] or "",
                             "last_name": r["last_name"] or "", "username": r["username"] or "",
                             "viewed_at": iso(r["first_viewed_at"], tz), "last_viewed_at": iso(r["viewed_at"], tz),
                             "after_min": ((r["first_viewed_at"] or 0) - (r["posted_at"] or 0)) // 60,
                             "reaction": r["reaction"] or "", "who": who_plain(r),
                             "close_friend": int(bool(r["close_friend"]))})
    return _write_export(tb, f"export-{what}", cfg)
