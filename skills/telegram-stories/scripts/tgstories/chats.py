"""Chat segments: "only the members of this group".

A segment of kind `chat` names a group (`--source @group`, a t.me link or the numeric id -100…). Its members
are read from Telegram and stored as the segment's member list; the service reads the list again every
`poll.chat_segments_s` seconds. A rule whose audience is such a segment queues only people on that list.

The list can be minutes old, and people leave groups: right before an automatic message goes out, the
sender asks Telegram again whether this one person is in the group now. Not a member, or no clear answer —
no message (`is_member` returns None for "could not tell", and the sender treats that as "no").
"""
from __future__ import annotations

import json
import logging
import re

from . import db

log = logging.getLogger("telegram-stories")

KIND = "chat"
_LINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,64})/?$", re.IGNORECASE)


def normalize_source(source: str | None) -> str:
    s = (source or "").strip()
    if not s:
        raise SystemExit("a chat segment needs --source: @group, https://t.me/group or the numeric id -100…")
    if "/+" in s or "joinchat" in s.lower():
        raise SystemExit("an invite link does not identify a chat — use its @username or numeric id")
    m = _LINK.match(s)
    if m:
        return "@" + m.group(1)
    if s.lstrip("-").isdigit():
        return str(int(s))
    if re.fullmatch(r"@?[A-Za-z0-9_]{4,64}", s):
        return "@" + s.lstrip("@")
    raise SystemExit(f"{source!r}: use @group, https://t.me/group or the numeric id -100…")


def info(con, name: str) -> dict:
    """What the last refresh learned: title, chat id, member count, time, error."""
    raw = db.get_state(con, f"chat_segment:{name}")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _save(con, name: str, data: dict) -> None:
    db.set_state(con, f"chat_segment:{name}", db.dumps(data))


def chat_segments(con, names=None) -> list:
    rows = con.execute("SELECT * FROM segments WHERE kind=? ORDER BY name", (KIND,)).fetchall()
    return [r for r in rows if names is None or r["name"] in names]


async def _peer(api, con, seg):
    """The chat as an input peer. The access hash learned once is kept, so later refreshes and checks need
    no search through the dialogs."""
    known = info(con, seg["name"])
    if known.get("source") == seg["source"] and known.get("peer"):
        return api.input_chat(known["peer"])
    ent = await api.find_chat(seg["source"])
    peer = api.chat_ref(ent)
    known.update({"source": seg["source"], "peer": peer, "title": getattr(ent, "title", None)})
    _save(con, seg["name"], known)
    return api.input_chat(peer)


async def refresh(api, con, seg) -> dict:
    """Read the member list of one chat segment into segment_members. On failure the old list stays and the
    error is recorded (an empty list from a failed read must not look like "nobody is a member")."""
    name = seg["name"]
    data = info(con, name)
    try:
        peer = await _peer(api, con, seg)
        ids = await api.chat_member_ids(peer)
    except Exception as exc:  # noqa: BLE001 — recorded and shown in `segment list` / previews
        data = info(con, name)
        data.update({"error": f"{type(exc).__name__}: {exc}"[:200], "error_at": db.now()})
        _save(con, name, data)
        log.warning("chat segment %s not refreshed: %s", name, data["error"])
        return data
    now = db.now()
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM segment_members WHERE segment=?", (name,))
        con.executemany("INSERT OR IGNORE INTO segment_members(segment,user_id,added_at) VALUES(?,?,?)",
                        [(name, uid, now) for uid in ids])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    data = info(con, name)
    data.update({"members": len(ids), "refreshed_at": now})
    data.pop("error", None)
    data.pop("error_at", None)
    _save(con, name, data)
    return data


async def refresh_all(api, con, names=None) -> list:
    return [(seg["name"], await refresh(api, con, seg)) for seg in chat_segments(con, names)]


def chat_segment_of(con, spec: dict) -> str | None:
    """The chat segment a rule's audience depends on, if any."""
    a = spec.get("audience") or {}
    if a.get("mode") != "segment":
        return None
    row = con.execute("SELECT kind FROM segments WHERE name=?", (a.get("segment"),)).fetchone()
    return a["segment"] if row and row["kind"] == KIND else None


async def is_member(api, con, name: str, user_peer, user_id: int) -> bool | None:
    """Ask Telegram now. True / False, or None when it cannot tell (no rights, network, unknown chat)."""
    seg = con.execute("SELECT * FROM segments WHERE name=? AND kind=?", (name, KIND)).fetchone()
    if seg is None:
        return None
    try:
        peer = await _peer(api, con, seg)
        ok = await api.chat_has_member(peer, user_peer, user_id)
    except Exception as exc:  # noqa: BLE001 — "could not tell" is never "yes"
        log.warning("chat membership of %s in %s unknown: %s", user_id, name, type(exc).__name__)
        return None
    if ok is False:
        con.execute("DELETE FROM segment_members WHERE segment=? AND user_id=?", (name, user_id))
    elif ok is True:
        con.execute("INSERT OR IGNORE INTO segment_members(segment,user_id,added_at) VALUES(?,?,?)",
                    (name, user_id, db.now()))
    return ok
