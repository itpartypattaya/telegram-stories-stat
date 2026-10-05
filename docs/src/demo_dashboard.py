"""Build the dashboard on made-up data (for README screenshots; no real person is in it).

    uv run --with pillow python docs/src/demo_dashboard.py OUT_DIR [--locale en|ru]

Writes OUT_DIR/dashboard-demo.<locale>.html. Pillow only draws the fake story thumbnails.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "telegram-stories" / "scripts"))

FIRST = ["Alex", "Maria", "Ivan", "Olga", "Sam", "Nina", "Leo", "Kate", "Mark", "Anna", "Tom", "Vera", "Max", "Lena",
         "Dan", "Zoe", "Paul", "Ira", "Nick", "Eva", "Igor", "Lisa", "Oleg", "Mila", "Ben", "Sofia", "Ilya", "Dina"]
LAST = ["Smith", "Petrova", "Brown", "Kim", "Novak", "Garcia", "Lee", "Weber", "Rossi", "Silva", "Orlova", "Young",
        "Klein", "Moreau", "Sato", "Ivanova", "Berg", "Costa", "Fox", "Hart", "", "", ""]
CAPTIONS = ["Morning coffee and plans for the week", "Behind the scenes of today's shoot", "New guide is out — part 1",
            "Part 2: the checklist #guide", "Sunset run, 8 km", "Q&A: ask me anything", "Event tonight, 19:00",
            "Three tools I use every day", "Before / after", "Weekend market finds", "", "", "Thank you for 1k!",
            "Workshop recap", "What I read this month", "New office tour", "Poll: which cover?"]
REACTIONS = ["❤", "🔥", "👍", "😂", "😍", "👏", "custom:5368324170671202286"]


def thumb(path: Path, seed: int) -> None:
    from PIL import Image, ImageDraw
    rnd = random.Random(seed)
    w, h = 270, 480
    a = [rnd.randint(40, 230) for _ in range(3)]
    b = [rnd.randint(40, 230) for _ in range(3)]
    img = Image.new("RGB", (w, h))
    px = ImageDraw.Draw(img)
    for y in range(h):
        k = y / h
        px.line([(0, y), (w, y)], fill=tuple(int(a[i] * (1 - k) + b[i] * k) for i in range(3)))
    for _ in range(3):
        r = rnd.randint(30, 110)
        x, y = rnd.randint(0, w), rnd.randint(0, h)
        px.ellipse([x - r, y - r, x + r, y + r], fill=tuple(min(255, c + 40) for c in a))
    img.save(path, "JPEG", quality=70)


def build(home: Path, locale: str, seed: int = 7, period: str | None = None) -> Path:
    os.environ["HERMES_HOME"] = str(home)
    (home / "telegram-stories.json").write_text(json.dumps({
        "timezone": "UTC", "locale": locale, "autoresponder": {"enabled": True}}), encoding="utf-8")
    from tgstories import config, dashboard, db
    rnd = random.Random(seed)
    con = db.connect()
    now = db.now()
    owner = 1
    db.set_meta(con, "owner_id", owner)
    db.set_state(con, "last_poll_at", now - 40)
    con.execute("INSERT INTO peers(peer_id,kind,title,username,added_at) VALUES(1,'self','Alex Demo','alexdemo',?)",
                (now,))
    people = []
    for uid in range(1000, 1420):
        f, last = rnd.choice(FIRST), rnd.choice(LAST)
        user = (f + (last or str(rnd.randint(1, 99)))).lower() if rnd.random() < 0.7 else None
        mutual = rnd.random() < 0.3
        contact = mutual or rnd.random() < 0.25
        # loyalty: how likely the person views a story; joined: when they started watching
        loyalty = rnd.choice([0.92, 0.8, 0.55, 0.35, 0.15, 0.06])
        joined = now - int(rnd.uniform(0, 430) * 86400) if rnd.random() < 0.85 else now - int(rnd.uniform(0, 20) * 86400)
        left = now - int(rnd.uniform(25, 200) * 86400) if rnd.random() < 0.18 else None
        people.append((uid, loyalty, joined, left))
        db.upsert_person(con, {"user_id": uid, "access_hash": 1, "first_name": f, "last_name": last or None,
                               "username": user, "is_contact": int(contact), "mutual": int(mutual),
                               "close_friend": int(mutual and rnd.random() < 0.2), "premium": 0, "bot": 0,
                               "deleted": 0, "paid_stars": 0})
    thumbs = config.data_dir() / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    t = now - 420 * 86400
    sid = 0
    while t < now - 3600:
        sid += 1
        hour = rnd.choice([8, 9, 12, 13, 18, 19, 19, 20, 20, 21, 21, 22])
        posted = (t // 86400) * 86400 + hour * 3600 + rnd.randint(0, 3000)
        quality = rnd.lognormvariate(0, 0.25)
        cap = rnd.choice(CAPTIONS)
        kind = "video" if rnd.random() < 0.35 else "photo"
        viewers = []
        for uid, loyalty, joined, left in people:
            if joined > posted or (left and posted > left):
                continue
            if rnd.random() < min(0.97, loyalty * quality):
                lag = int(rnd.expovariate(1 / (1.6 * 3600)) if rnd.random() < 0.8 else rnd.uniform(4, 40) * 3600)
                if posted + lag < now:
                    viewers.append((uid, posted + lag))
        reactions = 0
        for uid, at in viewers:
            r = rnd.choice(REACTIONS) if rnd.random() < 0.09 else None
            reactions += bool(r)
            con.execute("INSERT INTO views(peer_id,story_id,user_id,first_viewed_at,viewed_at,reaction,blocked,"
                        "hidden_from,seen_at) VALUES(?,?,?,?,?,?,0,0,?)", (owner, sid, uid, at, at, r, at))
        views = len(viewers) + rnd.randint(0, 4)
        name = f"{owner}_{sid}.jpg"
        thumb(thumbs / name, seed * 1000 + sid)
        con.execute("INSERT INTO stories(peer_id,story_id,posted_at,expire_at,pinned,media_kind,duration,caption,"
                    "views,reactions,forwards,viewers_listed,list_available,deleted,first_seen,last_synced,"
                    "list_synced,thumb) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,0,?,?,?,?)",
                    (owner, sid, posted, posted + 86400, int(rnd.random() < 0.4), kind,
                     12.0 if kind == "video" else None, cap or None, views, reactions, rnd.randint(0, 6),
                     len(viewers), posted, now, now, name))
        for uid, at in rnd.sample(viewers, min(len(viewers), rnd.randint(0, 3))):
            con.execute("INSERT OR IGNORE INTO story_replies(peer_id,story_id,user_id,msg_id,at,length) "
                        "VALUES(?,?,?,?,?,?)", (owner, sid, uid, sid * 100 + uid % 100, at + 60, 12))
        t += int(rnd.expovariate(1 / (2.6 * 86400))) + 3600
    db.refresh_first_seen(con)
    # a channel: counts only
    con.execute("INSERT INTO peers(peer_id,kind,title,username,added_at) VALUES(-100,'channel','Demo Channel',"
                "'demochannel',?)", (now,))
    for k in range(1, 13):
        posted = now - (13 - k) * 9 * 86400
        con.execute("INSERT INTO stories(peer_id,story_id,posted_at,expire_at,media_kind,caption,views,reactions,"
                    "forwards,list_available,deleted) VALUES(-100,?,?,?,'photo',?,?,?,?,0,0)",
                    (k, posted, posted + 86400, rnd.choice(CAPTIONS) or None, rnd.randint(300, 900),
                     rnd.randint(5, 40), rnd.randint(0, 12)))
    # two rules: one sending, one in test mode
    for rid, name, status, spec, n_sent in (
            (1, "Guide for the last part", "active", {"action": {"type": "dm", "text": "…"}, "scope": "contacts"}, 23),
            (2, "Tell me when Maria views", "shadow", {"action": {"type": "notify"}}, 0)):
        con.execute("INSERT INTO rules(id,name,status,spec,digest,created_at) VALUES(?,?,?,?,?,?)",
                    (rid, name, status, json.dumps(spec), "x", now))
        for k in range(n_sent):
            con.execute("INSERT INTO deliveries(rule_id,user_id,status,created_at,sent_at,replied_at) "
                        "VALUES(?,?,'sent',?,?,?)", (rid, 1000 + k, now, now, now if k % 3 == 0 else None))
        if status == "shadow":
            for k in range(4):
                con.execute("INSERT INTO deliveries(rule_id,user_id,status,created_at) VALUES(?,?,'shadow',?)",
                            (rid, 1100 + k, now))
    cfg = config.load_config()
    return dashboard.build(cfg, home / "out.html", con=con, period=period)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--locale", default="en", choices=["en", "ru"])
    ap.add_argument("--period", choices=["30d", "90d", "1y", "all"], help="tab open first")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        page = build(Path(tmp), args.locale, period=args.period)
        out = Path(args.out_dir) / f"dashboard-demo.{args.locale}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(page.read_bytes())
        print(out, f"{out.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
