"""Collection: live polling of active stories, profile stories, channels, and history import.

Telegram sends no event when someone views a story, so the collector polls:
  every minute     counters of active stories, one request for all of them
  on growth        the viewer list, newest first, until the first row already stored
  every 5 minutes  newly posted stories
  every 30 minutes profile (pinned) stories younger than N days — they keep collecting views
  every 15 minutes channel stories (counts, reactions/reposts; Telegram hides channel viewers)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from . import config, db, tg

log = logging.getLogger("telegram-stories")

SELF = "me"


class Collector:
    def __init__(self, api, con, cfg: dict, owner_id: int, *, on_view=None, sleep=asyncio.sleep):
        self.api = api
        self.con = con
        self.cfg = cfg
        self.owner_id = owner_id
        self.on_view = on_view          # callback(kind, row) for the rules engine
        self.sleep = sleep
        self.poll = cfg.get("poll", {})
        self._last_reaction_refresh: dict = {}
        self._last_snapshot: dict = {}
        self._channels: dict = {}       # peer_id -> input entity

    # ── stories ────────────────────────────────────────────────────────────
    def _ingest_users(self, users) -> None:
        for u in users or []:
            row = tg.user_row(u)
            if row:
                db.upsert_person(self.con, row)

    def _ingest_stories(self, items, peer_id: int) -> list[int]:
        ids = []
        for s in items or []:
            row = tg.story_row(s, peer_id)
            if row is None:
                continue
            new = db.upsert_story(self.con, row)
            if new and not row.get("deleted"):
                log.info("new story %s/%s", peer_id, row["story_id"])
                if self.on_view:
                    self.on_view("story", row)
            ids.append(row["story_id"])
        return ids

    async def refresh_active(self, peer=SELF, peer_id: int | None = None) -> list[int]:
        """Currently active (not expired) stories of a peer."""
        peer_id = peer_id or self.owner_id
        res = await self.api.peer_stories(peer)
        self._ingest_users(getattr(res, "users", None))
        ps = getattr(res, "stories", None)
        items = getattr(ps, "stories", None) or []
        return self._ingest_stories(items, peer_id)

    def active_ids(self, peer_id: int | None = None) -> list[int]:
        peer_id = peer_id or self.owner_id
        now = db.now()
        rows = self.con.execute("SELECT story_id FROM stories WHERE peer_id=? AND deleted=0 AND expire_at>?",
                                (peer_id, now)).fetchall()
        return [r[0] for r in rows]

    def pinned_recent_ids(self, peer_id: int | None = None) -> list[int]:
        peer_id = peer_id or self.owner_id
        now = db.now()
        age = int(self.poll.get("pinned_max_age_days", 30)) * 86400
        rows = self.con.execute(
            "SELECT story_id FROM stories WHERE peer_id=? AND deleted=0 AND pinned=1 AND expire_at<=? "
            "AND posted_at>?", (peer_id, now, now - age)).fetchall()
        return [r[0] for r in rows]

    # ── counters and viewer lists ──────────────────────────────────────────
    async def poll_counters(self, ids: list[int], peer=SELF, peer_id: int | None = None,
                            force_lists: bool = False) -> int:
        """Refresh counters; read viewer lists where they changed. Returns new viewers found."""
        peer_id = peer_id or self.owner_id
        if not ids:
            return 0
        res = await self.api.stories_views(peer, ids)
        self._ingest_users(getattr(res, "users", None))
        found = 0
        now = db.now()
        for sid, sv in zip(ids, getattr(res, "views", None) or []):
            old = self.con.execute("SELECT views, reactions, forwards, viewers_listed, posted_at, list_available, "
                                   "list_synced FROM stories WHERE peer_id=? AND story_id=?",
                                   (peer_id, sid)).fetchone()
            views = getattr(sv, "views_count", None)
            reactions = getattr(sv, "reactions_count", None)
            forwards = getattr(sv, "forwards_count", None)
            rc_json = None
            if getattr(sv, "reactions", None):
                rc = {}
                for r in sv.reactions:
                    key = tg.reaction_str(getattr(r, "reaction", None)) or "?"
                    rc[key] = rc.get(key, 0) + int(getattr(r, "count", 0) or 0)
                rc_json = json.dumps(rc, ensure_ascii=False, sort_keys=True)
            self.con.execute("UPDATE stories SET views=?, reactions=COALESCE(?, reactions), "
                             "forwards=COALESCE(?, forwards), reactions_json=COALESCE(?, reactions_json), "
                             "last_synced=? WHERE peer_id=? AND story_id=?",
                             (views, reactions, forwards, rc_json, now, peer_id, sid))
            self._maybe_snapshot(peer_id, sid, old, views, reactions, forwards, now)
            if peer_id != self.owner_id:
                continue  # channels: Telegram does not list viewers
            # compare with the previous counter, not with the listed rows: hidden (incognito) or
            # deleted viewers can keep the two apart forever and would cost a request every minute
            views_grew = (old is None or old["list_synced"] is None
                          or (views or 0) > (old["views"] or 0))
            reactions_changed = old is not None and (reactions or 0) != (old["reactions"] or 0)
            did_full = False
            if force_lists or views_grew:
                first_time = old is None or old["list_synced"] is None
                found += await self.fetch_views(sid, peer=peer, peer_id=peer_id, full=first_time)
                did_full = first_time
            if (reactions_changed or force_lists) and not did_full:
                last = self._last_reaction_refresh.get(sid, 0)
                if force_lists or now - last >= int(self.poll.get("reactions_refresh_s", 600)):
                    self._last_reaction_refresh[sid] = now
                    found += await self.fetch_views(sid, peer=peer, peer_id=peer_id, full=True)
        return found

    def _maybe_snapshot(self, peer_id, sid, old, views, reactions, forwards, now) -> None:
        posted = old["posted_at"] if old is not None else None
        if posted is None or now - posted > 48 * 3600:
            return
        last = self._last_snapshot.get((peer_id, sid), 0)
        if now - last < int(self.poll.get("snapshot_s", 600)):
            return
        self._last_snapshot[(peer_id, sid)] = now
        self.con.execute("INSERT OR IGNORE INTO story_snapshots(peer_id,story_id,ts,views,reactions,forwards) "
                         "VALUES(?,?,?,?,?,?)", (peer_id, sid, now, views, reactions, forwards))

    async def fetch_views(self, story_id: int, *, peer=SELF, peer_id: int | None = None,
                          full: bool = False, pause: float | None = None) -> int:
        """Read the viewer list newest-first. Incremental mode stops at the first row already stored."""
        peer_id = peer_id or self.owner_id
        pause = self.poll.get("page_pause_s", 0.7) if pause is None else pause
        offset, new_rows, pages = "", 0, 0
        total = views_count = None
        while True:
            res = await self.api.views_list(peer, story_id, offset=offset, limit=100)
            pages += 1
            total = getattr(res, "count", None)
            views_count = getattr(res, "views_count", None)
            self._ingest_users(getattr(res, "users", None))
            hit_known = False
            for item in getattr(res, "views", None) or []:
                parsed = tg.view_item(item, peer_id, story_id)
                if parsed is None:
                    continue
                kind, row = parsed
                if kind == "public":
                    if row.get("actor_id"):
                        self.con.execute(
                            "INSERT OR REPLACE INTO public_interactions(peer_id,story_id,actor_id,kind,value,at) "
                            "VALUES(?,?,?,?,?,?)",
                            (row["peer_id"], row["story_id"], row["actor_id"], row["kind"], row["value"], row["at"]))
                    continue
                outcome = db.upsert_view(self.con, row)
                if outcome == "new":
                    new_rows += 1
                    if self.on_view:
                        self.on_view("view", row)
                elif outcome == "changed" and self.on_view:
                    self.on_view("view_changed", row)
                elif outcome == "same" and not full:
                    hit_known = True
                    break
            offset = getattr(res, "next_offset", None)
            if hit_known or not offset:
                break
            await self.sleep(pause)
        listed = db.refresh_story_listing(self.con, peer_id, story_id)
        available = None
        if total is not None:
            available = 1 if (total or 0) > 0 or (views_count or 0) == 0 else 0
        self.con.execute("UPDATE stories SET list_available=COALESCE(?, list_available), list_synced=?, "
                         "views=COALESCE(views, ?) WHERE peer_id=? AND story_id=?",
                         (available, db.now(), views_count, peer_id, story_id))
        if new_rows:
            log.info("story %s: +%d viewer(s), %d listed", story_id, new_rows, listed)
        return new_rows

    # ── channels ───────────────────────────────────────────────────────────
    async def channel_peer(self, ref: str):
        ent = await self.api.entity(ref)
        peer_id = -int(ent.id)
        self._channels[peer_id] = ent
        self.con.execute("INSERT INTO peers(peer_id,kind,title,username,added_at) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(peer_id) DO UPDATE SET title=excluded.title, username=excluded.username",
                         (peer_id, "channel", getattr(ent, "title", None), getattr(ent, "username", None),
                          db.now()))
        return ent, peer_id

    async def poll_channel(self, ref: str, *, with_stats: bool = False) -> None:
        ent, peer_id = await self.channel_peer(ref)
        await self.refresh_active(ent, peer_id)
        ids = self.active_ids(peer_id)
        if ids:
            await self.poll_counters(ids, peer=ent, peer_id=peer_id)
            for sid in ids:
                await self.channel_interactions(ent, peer_id, sid)
                if with_stats:
                    await self.channel_stats(ent, peer_id, sid)

    async def channel_interactions(self, ent, peer_id: int, story_id: int) -> int:
        n, offset = 0, None
        for _ in range(20):
            try:
                res = await self.api.reactions_list(ent, story_id, offset=offset, limit=100)
            except Exception as exc:  # noqa: BLE001 — no rights / feature unavailable
                log.info("channel reactions list unavailable for %s: %s", story_id, type(exc).__name__)
                return n
            self._ingest_users(getattr(res, "users", None))
            for r in getattr(res, "reactions", None) or []:
                row = tg.reaction_item(r, peer_id, story_id)
                if row and row.get("actor_id"):
                    self.con.execute(
                        "INSERT OR REPLACE INTO public_interactions(peer_id,story_id,actor_id,kind,value,at) "
                        "VALUES(?,?,?,?,?,?)",
                        (row["peer_id"], row["story_id"], row["actor_id"], row["kind"], row["value"], row["at"]))
                    n += 1
            offset = getattr(res, "next_offset", None)
            if not offset:
                break
        return n

    async def channel_stats(self, ent, peer_id: int, story_id: int) -> None:
        try:
            graphs = await self.api.story_stats(ent, story_id)
        except Exception as exc:  # noqa: BLE001 — stats need enough subscribers
            log.info("story stats unavailable for %s: %s", story_id, type(exc).__name__)
            return
        for kind, data in graphs.items():
            self.con.execute("INSERT OR REPLACE INTO channel_story_graphs(peer_id,story_id,kind,fetched_at,json) "
                             "VALUES(?,?,?,?,?)", (peer_id, story_id, kind, db.now(), data))

    # ── history import ─────────────────────────────────────────────────────
    async def archive_items(self, peer=SELF):
        items, offset_id = [], 0
        for _ in range(200):
            res = await self.api.archive(peer, offset_id=offset_id, limit=100)
            self._ingest_users(getattr(res, "users", None))
            batch = getattr(res, "stories", None) or []
            if not batch:
                break
            items.extend(batch)
            offset_id = min(int(s.id) for s in batch)
            if len(batch) < 100:
                break
            await self.sleep(self.cfg.get("backfill", {}).get("pause_s", 2.0))
        return items

    async def backfill(self, *, peer=SELF, peer_id: int | None = None, limit: int | None = None,
                       thumbs: bool = True, refetch: bool = False, progress=print) -> dict:
        peer_id = peer_id or self.owner_id
        pause = float(self.cfg.get("backfill", {}).get("pause_s", 2.0))
        items = await self.archive_items(peer)
        if peer_id == self.owner_id:
            # active stories are in the archive too; pinned ones are not always — merge both
            try:
                res = await self.api.pinned(peer, offset_id=0, limit=100)
                known = {int(s.id) for s in items}
                items.extend(s for s in getattr(res, "stories", None) or [] if int(s.id) not in known)
            except Exception:  # noqa: BLE001
                pass
        self._ingest_stories(items, peer_id)
        items.sort(key=lambda s: int(s.id))
        done = skipped = viewers = 0
        for s in items:
            sid = int(s.id)
            row = self.con.execute("SELECT backfilled_at FROM stories WHERE peer_id=? AND story_id=?",
                                   (peer_id, sid)).fetchone()
            if row is None:
                continue
            if row["backfilled_at"] and not refetch:
                skipped += 1
                continue
            if limit is not None and done >= limit:
                break
            if peer_id == self.owner_id:
                viewers += await self.fetch_views(sid, peer=peer, peer_id=peer_id, full=True, pause=pause)
            else:
                await self.channel_interactions(peer, peer_id, sid)
                await self.channel_stats(peer, peer_id, sid)
            if thumbs:
                await self.save_thumb(s, peer_id)
            self.con.execute("UPDATE stories SET backfilled_at=? WHERE peer_id=? AND story_id=?",
                             (db.now(), peer_id, sid))
            done += 1
            if progress and (done % 10 == 0 or done == 1):
                progress(f"  {done} stories imported…")
            await self.sleep(pause)
        db.refresh_first_seen(self.con)
        return {"stories": len(items), "imported": done, "already": skipped, "viewer_rows": viewers}

    async def save_thumb(self, story, peer_id: int) -> None:
        client = getattr(self.api, "client", None)
        media = getattr(story, "media", None)
        if client is None or media is None:
            return
        target = config.data_dir() / "thumbs" / f"{peer_id}_{int(story.id)}.jpg"
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        sizes = []
        photo = getattr(media, "photo", None)
        doc = getattr(media, "document", None)
        for sz in (getattr(photo, "sizes", None) or getattr(doc, "thumbs", None) or []):
            w, h = getattr(sz, "w", 0) or 0, getattr(sz, "h", 0) or 0
            if w and h and max(w, h) <= 480:
                sizes.append((max(w, h), sz))
        thumb = max(sizes, key=lambda x: x[0])[1] if sizes else 0
        try:
            await client.download_media(media, file=str(target), thumb=thumb)
            self.con.execute("UPDATE stories SET thumb=? WHERE peer_id=? AND story_id=?",
                             (target.name, peer_id, int(story.id)))
        except Exception as exc:  # noqa: BLE001 — thumbnails are optional
            log.info("thumbnail for %s skipped: %s", story.id, type(exc).__name__)


# ── entry points used by the CLI ───────────────────────────────────────────

async def _open(cfg):
    client = await tg.connect(cfg)
    con = db.connect()
    me = await client.get_me()
    db.set_meta(con, "owner_id", me.id)
    db.set_meta(con, "owner_premium", int(bool(getattr(me, "premium", False))))
    con.execute("INSERT INTO peers(peer_id,kind,title,username,added_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(peer_id) DO NOTHING",
                (me.id, "self", " ".join(x for x in (me.first_name, me.last_name) if x), me.username, db.now()))
    return client, con, me


async def run_once(cfg: dict, verbose: bool = False) -> int:
    client, con, me = await _open(cfg)
    try:
        col = Collector(tg.Api(client), con, cfg, me.id)
        await col.refresh_active()
        ids = col.active_ids()
        found = await col.poll_counters(ids)
        db.set_state(con, "last_poll_at", db.now())
        if verbose:
            print(f"{len(ids)} active stories, {found} new viewer(s)")
        return 0
    finally:
        await client.disconnect()


async def run_backfill(cfg: dict, channel: str | None = None, limit: int | None = None,
                       thumbs: bool = True, refetch: bool = False) -> int:
    client, con, me = await _open(cfg)
    started = time.time()
    try:
        col = Collector(tg.Api(client), con, cfg, me.id)
        if channel:
            ent, peer_id = await col.channel_peer(channel)
            print(f"Importing stories of {getattr(ent, 'title', channel)}…")
            res = await col.backfill(peer=ent, peer_id=peer_id, limit=limit, thumbs=thumbs, refetch=refetch)
        else:
            if not getattr(me, "premium", False):
                print("Note: without Premium, Telegram keeps viewer lists only 24 h after a story expires.")
            print("Importing your stories (resumable — safe to stop and re-run)…")
            res = await col.backfill(limit=limit, thumbs=thumbs, refetch=refetch)
        print(f"Done in {int(time.time() - started)} s: {res['stories']} stories found, "
              f"{res['imported']} imported now, {res['already']} were already imported, "
              f"{res['viewer_rows']} new viewer rows.")
        return 0
    finally:
        await client.disconnect()
