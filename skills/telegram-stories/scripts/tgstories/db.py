"""SQLite storage: schema, migrations and small helpers.

All timestamps are UTC epoch seconds. The database runs in WAL mode so the
collector service and the CLI can read and write it at the same time.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from . import config

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS peers (
  peer_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                 -- self | channel
  title TEXT, username TEXT,
  tracked INTEGER DEFAULT 1,
  added_at INTEGER
);

CREATE TABLE IF NOT EXISTS stories (
  peer_id INTEGER NOT NULL,
  story_id INTEGER NOT NULL,
  posted_at INTEGER, expire_at INTEGER,
  pinned INTEGER, privacy TEXT,
  media_kind TEXT, duration REAL,
  caption TEXT, entities TEXT, areas TEXT, thumb TEXT,
  views INTEGER, reactions INTEGER, forwards INTEGER, reactions_json TEXT,
  viewers_listed INTEGER DEFAULT 0,   -- rows in `views` for this story
  list_available INTEGER,             -- 1 Telegram returned a viewer list, 0 it did not, NULL unknown
  deleted INTEGER DEFAULT 0,
  first_seen INTEGER, last_synced INTEGER, list_synced INTEGER, backfilled_at INTEGER,
  pulse_sent_at INTEGER,
  PRIMARY KEY (peer_id, story_id)
);
CREATE INDEX IF NOT EXISTS stories_time ON stories(peer_id, posted_at);

CREATE TABLE IF NOT EXISTS story_snapshots (
  peer_id INTEGER, story_id INTEGER, ts INTEGER,
  views INTEGER, reactions INTEGER, forwards INTEGER,
  PRIMARY KEY (peer_id, story_id, ts)
);

CREATE TABLE IF NOT EXISTS channel_story_graphs (
  peer_id INTEGER, story_id INTEGER, kind TEXT, fetched_at INTEGER, json TEXT,
  PRIMARY KEY (peer_id, story_id, kind)
);

CREATE TABLE IF NOT EXISTS people (
  user_id INTEGER PRIMARY KEY,
  access_hash INTEGER,
  first_name TEXT, last_name TEXT,
  username TEXT, usernames TEXT,
  is_contact INTEGER, mutual INTEGER, close_friend INTEGER,
  premium INTEGER, bot INTEGER, deleted INTEGER,
  paid_stars INTEGER,
  has_dialog INTEGER, dialog_checked_at INTEGER,
  first_seen_at INTEGER, updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS people_history (
  user_id INTEGER, field TEXT, old TEXT, new TEXT, changed_at INTEGER
);
CREATE INDEX IF NOT EXISTS people_history_user ON people_history(user_id);

CREATE TABLE IF NOT EXISTS views (
  peer_id INTEGER, story_id INTEGER, user_id INTEGER,
  first_viewed_at INTEGER,            -- earliest date Telegram ever reported for this viewer
  viewed_at INTEGER,                  -- latest date Telegram reported
  reaction TEXT,
  blocked INTEGER, hidden_from INTEGER,
  seen_at INTEGER,                    -- when this collector first saw the row
  PRIMARY KEY (peer_id, story_id, user_id)
);
CREATE INDEX IF NOT EXISTS views_user ON views(user_id, first_viewed_at);
CREATE INDEX IF NOT EXISTS views_time ON views(peer_id, first_viewed_at);

CREATE TABLE IF NOT EXISTS view_events (
  peer_id INTEGER, story_id INTEGER, user_id INTEGER, viewed_at INTEGER, reaction TEXT,
  PRIMARY KEY (peer_id, story_id, user_id, viewed_at)
);

CREATE TABLE IF NOT EXISTS public_interactions (
  peer_id INTEGER, story_id INTEGER, actor_id INTEGER,
  kind TEXT,                          -- reaction | forward | repost
  value TEXT, at INTEGER,
  PRIMARY KEY (peer_id, story_id, actor_id, kind)
);

CREATE TABLE IF NOT EXISTS story_replies (
  peer_id INTEGER, story_id INTEGER, user_id INTEGER, msg_id INTEGER, at INTEGER, length INTEGER,
  PRIMARY KEY (user_id, msg_id)
);
CREATE INDEX IF NOT EXISTS story_replies_story ON story_replies(peer_id, story_id);

CREATE TABLE IF NOT EXISTS common_chats (
  user_id INTEGER, chat_id INTEGER, title TEXT, fetched_at INTEGER,
  PRIMARY KEY (user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS segments (
  name TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                 -- static | contacts | mutual | close_friends | status
  source TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS segment_members (
  segment TEXT, user_id INTEGER, added_at INTEGER,
  PRIMARY KEY (segment, user_id)
);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  status TEXT NOT NULL,               -- draft | shadow | active | paused | done
  spec TEXT NOT NULL,                 -- JSON
  digest TEXT NOT NULL,
  bound_story_id INTEGER,             -- for stories.mode = "next"
  created_at INTEGER, updated_at INTEGER, activated_at INTEGER,
  paused_reason TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
  rule_id INTEGER, user_id INTEGER,
  peer_id INTEGER, story_id INTEGER,
  status TEXT,                        -- shadow | queued | sent | skipped | failed
  reason TEXT, variant INTEGER,
  created_at INTEGER, due_at INTEGER, sent_at INTEGER, msg_id INTEGER, replied_at INTEGER,
  attempts INTEGER DEFAULT 0,
  PRIMARY KEY (rule_id, user_id)
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(status, due_at);
CREATE INDEX IF NOT EXISTS deliveries_user ON deliveries(user_id, sent_at);

CREATE TABLE IF NOT EXISTS notifications (key TEXT PRIMARY KEY, sent_at INTEGER);
"""

SCHEMA_V2 = """
-- the counters at the moment the viewer list was last read: growth is measured against these, not against
-- the live counters that other refreshes keep overwriting (a refresh in between used to hide new viewers)
ALTER TABLE stories ADD COLUMN listed_views INTEGER;
ALTER TABLE stories ADD COLUMN listed_reactions INTEGER;
-- the rule as it was when the delivery was queued: a changed rule never sends with an old queue
ALTER TABLE deliveries ADD COLUMN rule_digest TEXT;
-- the scope a message was actually sent under: caps count history, not the rule's current spec
ALTER TABLE deliveries ADD COLUMN scope TEXT;
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA_V1),
    (2, SCHEMA_V2),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def connect(path: Path | None = None, *, create: bool = True) -> sqlite3.Connection:
    path = Path(path or config.db_path())
    if not create and not path.exists():
        raise SystemExit(f"no database at {path} — run scripts/install.py first")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    migrate(con)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return con


def migrate(con: sqlite3.Connection) -> int:
    current = con.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in MIGRATIONS:
        if version > current:
            con.executescript("BEGIN;" + sql + f"PRAGMA user_version={version};COMMIT;")
            current = version
    return current


def now() -> int:
    return int(time.time())


def get_meta(con, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con, key: str, value) -> None:
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, None if value is None else str(value)))


def get_state(con, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(con, key: str, value) -> None:
    con.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, None if value is None else str(value)))


def owner_id(con) -> int | None:
    v = get_meta(con, "owner_id")
    return int(v) if v else None


# ── upserts ────────────────────────────────────────────────────────────────

PEOPLE_TRACKED = ("first_name", "last_name", "username")


def upsert_person(con, p: dict, *, ts: int | None = None) -> None:
    """Insert or refresh a profile; name and username changes go to people_history."""
    ts = ts or now()
    old = con.execute("SELECT * FROM people WHERE user_id=?", (p["user_id"],)).fetchone()
    if old is None:
        cols = [c for c in p if c != "user_id"]
        con.execute(
            f"INSERT INTO people(user_id,{','.join(cols)},updated_at) VALUES(?,{','.join('?' * len(cols))},?)",
            (p["user_id"], *[p[c] for c in cols], ts))
        return
    for field in PEOPLE_TRACKED:
        if field in p and (old[field] or "") != (p.get(field) or ""):
            con.execute("INSERT INTO people_history(user_id,field,old,new,changed_at) VALUES(?,?,?,?,?)",
                        (p["user_id"], field, old[field], p.get(field), ts))
    sets, vals = [], []
    for c, v in p.items():
        if c == "user_id":
            continue
        if c == "access_hash" and not v:
            continue  # a "min" user object carries no usable hash — keep the stored one
        if v is None and c not in PEOPLE_TRACKED:
            continue  # unknown is not "no": an incomplete profile never erases a known flag (paid_stars!)
        sets.append(f"{c}=?")
        vals.append(v)
    sets.append("updated_at=?")
    vals.append(ts)
    con.execute(f"UPDATE people SET {','.join(sets)} WHERE user_id=?", (*vals, p["user_id"]))


def upsert_story(con, s: dict, *, ts: int | None = None) -> bool:
    """Insert or update a story row. Returns True when the story is new."""
    ts = ts or now()
    exists = con.execute("SELECT 1 FROM stories WHERE peer_id=? AND story_id=?",
                         (s["peer_id"], s["story_id"])).fetchone()
    if not exists:
        cols = list(s)
        con.execute(
            f"INSERT INTO stories({','.join(cols)},first_seen,last_synced) "
            f"VALUES({','.join('?' * len(cols))},?,?)",
            (*[s[c] for c in cols], ts, ts))
        return True
    keep_if_none = {"views", "reactions", "forwards", "reactions_json", "thumb"}
    sets, vals = [], []
    for c, v in s.items():
        if c in ("peer_id", "story_id"):
            continue
        if v is None and c in keep_if_none:
            continue
        sets.append(f"{c}=?")
        vals.append(v)
    sets.append("last_synced=?")
    vals.append(ts)
    con.execute(f"UPDATE stories SET {','.join(sets)} WHERE peer_id=? AND story_id=?",
                (*vals, s["peer_id"], s["story_id"]))
    return False


def upsert_view(con, v: dict, *, ts: int | None = None) -> str:
    """Record one viewer row. Returns 'new', 'changed' or 'same'."""
    ts = ts or now()
    key = (v["peer_id"], v["story_id"], v["user_id"])
    con.execute("INSERT OR IGNORE INTO view_events(peer_id,story_id,user_id,viewed_at,reaction) "
                "VALUES(?,?,?,?,?)", (*key, v["viewed_at"], v.get("reaction")))
    old = con.execute("SELECT first_viewed_at, viewed_at, reaction, blocked, hidden_from FROM views "
                      "WHERE peer_id=? AND story_id=? AND user_id=?", key).fetchone()
    if old is None:
        con.execute("INSERT INTO views(peer_id,story_id,user_id,first_viewed_at,viewed_at,reaction,blocked,"
                    "hidden_from,seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (*key, v["viewed_at"], v["viewed_at"], v.get("reaction"), v.get("blocked", 0),
                     v.get("hidden_from", 0), ts))
        return "new"
    first = min(old["first_viewed_at"] or v["viewed_at"], v["viewed_at"])
    latest = max(old["viewed_at"] or v["viewed_at"], v["viewed_at"])
    same = (old["viewed_at"] == latest and old["first_viewed_at"] == first
            and (old["reaction"] or None) == (v.get("reaction") or None)
            and int(old["blocked"] or 0) == int(v.get("blocked", 0) or 0)
            and int(old["hidden_from"] or 0) == int(v.get("hidden_from", 0) or 0))
    if same:
        return "same"
    con.execute("UPDATE views SET first_viewed_at=?, viewed_at=?, reaction=?, blocked=?, hidden_from=? "
                "WHERE peer_id=? AND story_id=? AND user_id=?",
                (first, latest, v.get("reaction"), v.get("blocked", 0), v.get("hidden_from", 0), *key))
    return "changed"


def refresh_story_listing(con, peer_id: int, story_id: int) -> int:
    n = con.execute("SELECT COUNT(*) FROM views WHERE peer_id=? AND story_id=?", (peer_id, story_id)).fetchone()[0]
    con.execute("UPDATE stories SET viewers_listed=? WHERE peer_id=? AND story_id=?", (n, peer_id, story_id))
    return n


def refresh_first_seen(con) -> None:
    """people.first_seen_at = the earliest story view we know of."""
    con.execute("""UPDATE people SET first_seen_at = (
                     SELECT MIN(first_viewed_at) FROM views WHERE views.user_id = people.user_id)
                   WHERE EXISTS (SELECT 1 FROM views WHERE views.user_id = people.user_id)""")


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
