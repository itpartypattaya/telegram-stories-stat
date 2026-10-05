"""Generates the README diagrams (EN + RU) in the baoyu-diagram style: dark slate, semantic colours,
monospace labels. Run: python docs/src/diagrams.py  → docs/src/*.svg (PNG: baoyu-diagram main.ts)."""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent

C = {  # fill, stroke
    "primary": ("rgba(8,51,68,0.4)", "#22d3ee"),
    "secondary": ("rgba(6,78,59,0.4)", "#34d399"),
    "tertiary": ("rgba(76,29,149,0.4)", "#a78bfa"),
    "accent": ("rgba(120,53,15,0.3)", "#fbbf24"),
    "alert": ("rgba(136,19,55,0.4)", "#fb7185"),
    "neutral": ("rgba(30,41,59,0.5)", "#94a3b8"),
    "highlight": ("rgba(59,130,246,0.3)", "#60a5fa"),
}


def head(w: int, h: int, title: str, sub: str) -> list[str]:
    markers = "".join(
        f'<marker id="a-{n}" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">'
        f'<polygon points="0 0, 10 3.5, 0 7" fill="{s}"/></marker>'
        f'<marker id="b-{n}" markerWidth="10" markerHeight="7" refX="1" refY="3.5" orient="auto">'
        f'<polygon points="10 0, 0 3.5, 10 7" fill="{s}"/></marker>'
        for n, (_, s) in C.items())
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">',
        "<style>text{font-family:'JetBrains Mono','DejaVu Sans Mono','Consolas','Menlo',monospace;}</style>",
        f'<defs><pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">'
        f'<path d="M 40 0 L 0 0 0 40" fill="none" stroke="#1e293b" stroke-width="0.5"/></pattern>{markers}</defs>',
        f'<rect width="{w}" height="{h}" fill="#0f172a"/><rect width="{w}" height="{h}" fill="url(#grid)"/>',
        f'<text x="30" y="40" fill="white" font-size="17" font-weight="700">{escape(title)}</text>',
        f'<text x="30" y="61" fill="#94a3b8" font-size="10.5">{escape(sub)}</text>',
    ]


def region(x, y, w, h, color, label):
    s = C[color][1]
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="none" stroke="{s}" stroke-width="1" '
            f'stroke-dasharray="8,4"/><text x="{x + 12}" y="{y + 17}" fill="{s}" font-size="10" '
            f'font-weight="600">{escape(label)}</text>')


def box(x, y, w, h, color, name, *subs, size=12):
    f, s = C[color]
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="#0f172a"/>',
           f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{f}" stroke="{s}" stroke-width="1.5"/>']
    lines = [l for l in subs if l]
    top = y + h / 2 - (len(lines) * 13) / 2 + 3
    out.append(f'<text x="{x + w / 2}" y="{top}" fill="white" font-size="{size}" font-weight="600" '
               f'text-anchor="middle">{escape(name)}</text>')
    for i, l in enumerate(lines):
        out.append(f'<text x="{x + w / 2}" y="{top + 16 + i * 13}" fill="#94a3b8" font-size="9.5" '
                   f'text-anchor="middle">{escape(l)}</text>')
    return "".join(out)


def cyl(x, y, w, name, sub, notes):
    f, s = C["tertiary"]
    rx, ry, h = w / 2, 12, 70 + 16 * len(notes)
    cx = x + rx
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#0f172a"/>',
           f'<ellipse cx="{cx}" cy="{y + h}" rx="{rx}" ry="{ry}" fill="#0f172a"/>',
           f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{f}"/>',
           f'<ellipse cx="{cx}" cy="{y + h}" rx="{rx}" ry="{ry}" fill="{f}" stroke="{s}" stroke-width="1.5"/>',
           f'<ellipse cx="{cx}" cy="{y}" rx="{rx}" ry="{ry}" fill="#0f172a"/>',
           f'<ellipse cx="{cx}" cy="{y}" rx="{rx}" ry="{ry}" fill="{f}" stroke="{s}" stroke-width="1.5"/>',
           f'<line x1="{x}" y1="{y}" x2="{x}" y2="{y + h}" stroke="{s}" stroke-width="1.5"/>',
           f'<line x1="{x + w}" y1="{y}" x2="{x + w}" y2="{y + h}" stroke="{s}" stroke-width="1.5"/>',
           f'<text x="{cx}" y="{y + 32}" fill="white" font-size="12" font-weight="600" text-anchor="middle">'
           f'{escape(name)}</text>',
           f'<text x="{cx}" y="{y + 48}" fill="#94a3b8" font-size="9.5" text-anchor="middle">{escape(sub)}</text>']
    for i, n in enumerate(notes):
        out.append(f'<text x="{cx}" y="{y + 64 + i * 14}" fill="#c4b5fd" font-size="9.5" '
                   f'text-anchor="middle">{escape(n)}</text>')
    return "".join(out)


def arrow(d, color, label="", lx=None, ly=None, both=False, dash=False, anchor="middle"):
    s = C[color][1]
    extra = ' stroke-dasharray="5,4"' if dash else ""
    start = f' marker-start="url(#b-{color})"' if both else ""
    out = f'<path d="{d}" fill="none" stroke="{s}" stroke-width="1.6"{extra} marker-end="url(#a-{color})"{start}/>'
    if label:
        out += (f'<text x="{lx}" y="{ly}" fill="{s}" font-size="9" text-anchor="{anchor}">'
                f'{escape(label)}</text>')
    return out


def legend(x, y, items):
    out = []
    for i, (color, text) in enumerate(items):
        f, s = C[color]
        out.append(f'<rect x="{x + i * 215}" y="{y}" width="14" height="10" rx="2" fill="{f}" stroke="{s}"/>')
        out.append(f'<text x="{x + i * 215 + 20}" y="{y + 9}" fill="#94a3b8" font-size="9.5">{escape(text)}</text>')
    return "".join(out)


# ── 1. architecture ────────────────────────────────────────────────────────

ARCH = {
    "en": dict(
        title="telegram-stories-stat · architecture",
        sub="1 long-running process · 1 Telegram connection · 0 language-model calls · ~65 MB RAM",
        tg="Telegram", mt="MTProto API", mt1="own session \"Hermes Stories\"", mt2="your stories and viewers",
        mt3="sends DMs only by active rules", bot="Bot API", bot1="your agent's bot", you="You",
        you1="native tables in chat", d="daemon.py · systemd user service",
        p1="Poller", p1s="counters every 60 s → viewer list on growth", p2="Listener",
        p2s="private replies to stories and auto-messages", p3="Rules → queue → sender",
        p3s="shadow→active · contacts · caps · quiet hours · stop", p4="Reports",
        p4s="pulse 2 h after posting · weekly / monthly digest",
        db="stories.db", dbs="SQLite · WAL", dbn=["stories · viewers · people", "rules · deliveries"],
        cli="stories.py (CLI)", clis="table · report · rule · export", ag="AI agent (Hermes)",
        ags="runs the CLI, pastes tables",
        l_req="3–5 req/min", l_upd="updates", l_dm="auto DMs (opt-in)", l_w="writes", l_rw="reads · rules",
        l_term="terminal", l_tab="tables (rich messages)", l_rep="pulse · digest",
        lg=[("primary", "Telegram I/O"), ("secondary", "service"), ("tertiary", "storage"),
            ("alert", "writes to people (opt-in)")]),
    "ru": dict(
        title="telegram-stories-stat · архитектура",
        sub="1 постоянный процесс · 1 соединение с Telegram · 0 вызовов нейросети · ~65 МБ памяти",
        tg="Telegram", mt="MTProto API", mt1="своя сессия «Hermes Stories»", mt2="ваши сторис и зрители",
        mt3="ЛС — только по активным правилам", bot="Bot API", bot1="бот вашего агента", you="Вы",
        you1="родные таблицы в чате", d="daemon.py · systemd-сервис пользователя",
        p1="Сборщик", p1s="счётчики раз в 60 с → список при росте", p2="Слушатель",
        p2s="ответы на сторис и на автосообщения", p3="Правила → очередь → отправка",
        p3s="shadow→active · контакты · лимиты · стоп", p4="Отчёты",
        p4s="пульс через 2 ч · дайджест неделя / месяц",
        db="stories.db", dbs="SQLite · WAL", dbn=["сторис · зрители · люди", "правила · доставки"],
        cli="stories.py (CLI)", clis="table · report · rule · export", ag="ИИ-агент (Hermes)",
        ags="запускает CLI, вставляет таблицы",
        l_req="3–5 запросов/мин", l_upd="события", l_dm="автоЛС (по желанию)", l_w="пишет", l_rw="чтение · правила",
        l_term="терминал", l_tab="таблицы (rich messages)", l_rep="пульс · дайджест",
        lg=[("primary", "ввод-вывод Telegram"), ("secondary", "сервис"), ("tertiary", "хранилище"),
            ("alert", "пишет людям (по желанию)")]),
}


def architecture(lang: str) -> str:
    t = ARCH[lang]
    W, H = 1020, 660
    o = head(W, H, t["title"], t["sub"])
    o.append(region(30, 90, 220, 500, "neutral", t["tg"]))
    o.append(region(370, 90, 390, 375, "secondary", t["d"]))
    # arrows first (under boxes); the 150 px gap leaves room for the labels
    o.append(arrow("M 232 158 L 385 158", "primary", t["l_req"], 308, 150, both=True))
    o.append(arrow("M 232 236 L 385 236", "primary", t["l_upd"], 308, 228))
    o.append(arrow("M 385 316 L 232 316", "alert", t["l_dm"], 308, 308))
    o.append(arrow("M 385 396 L 232 420", "highlight", t["l_rep"], 308, 395))
    o.append(arrow("M 140 455 L 140 512", "highlight"))
    o.append(arrow("M 743 158 L 808 182", "tertiary", t["l_w"], 770, 148))
    o.append(arrow("M 890 268 L 890 328", "tertiary", t["l_rw"], 898, 302, both=True, anchor="start"))
    o.append(arrow("M 890 452 L 890 384", "primary", t["l_term"], 898, 422, anchor="start"))
    o.append(arrow("M 890 516 L 890 548 L 232 548", "primary", t["l_tab"], 560, 540))
    # boxes
    o.append(box(50, 120, 180, 230, "primary", t["mt"], t["mt1"], "", t["mt2"], t["mt3"]))
    o.append(box(50, 395, 180, 60, "primary", t["bot"], t["bot1"]))
    o.append(box(50, 514, 180, 60, "highlight", t["you"], t["you1"]))
    o.append(box(387, 127, 356, 62, "secondary", t["p1"], t["p1s"]))
    o.append(box(387, 205, 356, 62, "secondary", t["p2"], t["p2s"]))
    o.append(box(387, 285, 356, 62, "alert", t["p3"], t["p3s"]))
    o.append(box(387, 365, 356, 62, "secondary", t["p4"], t["p4s"]))
    o.append(cyl(810, 150, 160, t["db"], t["dbs"], t["dbn"]))
    o.append(box(795, 330, 190, 54, "primary", t["cli"], t["clis"]))
    o.append(box(795, 454, 190, 62, "accent", t["ag"], t["ags"]))
    o.append(legend(30, 620, t["lg"]))
    o.append("</svg>")
    return "\n".join(o)


# ── 2. a story's life ───────────────────────────────────────────────────────

FLOW = {
    "en": dict(
        title="A story's life in the database",
        sub="what the service does by itself, minute by minute — nobody has to ask",
        s=[("You post a story", "t = 0", "primary"),
           ("Counters, every 60 s", "one request for all", "secondary"),
           ("Views grew?", "read the viewer list", "accent"),
           ("Saved", "who · when · reaction", "tertiary"),
           ("Pulse at +2 h", "vs your usual, same age", "highlight"),
           ("Digest", "Sunday 20:00 · 1st of month", "highlight")],
        n=["newest first, stops at the", "first viewer already stored"],
        loop="no → wait a minute", hist="Once at install: the whole history Telegram still keeps",
        hist2="209 stories since 07.2023 · 30 110 views · ~15 min · resumable",
        pin="after 48 h: profile (pinned) stories keep collecting, checked every 30 min"),
    "ru": dict(
        title="Жизнь сторис в базе",
        sub="что сервис делает сам, минута за минутой — никого не надо просить",
        s=[("Вы выложили сторис", "t = 0", "primary"),
           ("Счётчики раз в 60 с", "один запрос на все", "secondary"),
           ("Просмотры выросли?", "читаем список зрителей", "accent"),
           ("Сохранено", "кто · когда · реакция", "tertiary"),
           ("Пульс через 2 ч", "к обычному в том же возрасте", "highlight"),
           ("Дайджест", "вс 20:00 · 1-е число", "highlight")],
        n=["сверху вниз, до первого", "уже известного зрителя"],
        loop="нет → ждём минуту", hist="Один раз при установке: вся история, что хранит Telegram",
        hist2="209 сторис с 07.2023 · 30 110 просмотров · ~15 мин · с докачкой",
        pin="после 48 ч: сторис в профиле продолжают собираться, проверка раз в 30 мин"),
}


def flow(lang: str) -> str:
    t = FLOW[lang]
    W, H = 940, 470
    o = head(W, H, t["title"], t["sub"])
    xs = [30, 340, 650]
    w, h = 260, 66
    pos = [(xs[0], 110), (xs[1], 110), (xs[2], 110), (xs[2], 260), (xs[1], 260), (xs[0], 260)]
    # arrows
    o.append(arrow("M 290 143 L 338 143", "secondary"))
    o.append(arrow("M 600 143 L 648 143", "secondary"))
    o.append(arrow("M 780 176 L 780 258", "tertiary", "", 0, 0))
    o.append(arrow("M 650 293 L 602 293", "highlight"))
    o.append(arrow("M 340 293 L 292 293", "highlight"))
    o.append(arrow("M 760 110 L 760 96 L 470 96 L 470 108", "neutral", t["loop"], 615, 90, dash=True))
    for (x, y), (name, sub, color) in zip(pos, t["s"]):
        o.append(box(x, y, w, h, color, name, sub))
    o.append(f'<text x="790" y="200" fill="#c4b5fd" font-size="9" text-anchor="start">{escape(t["n"][0])}</text>')
    o.append(f'<text x="790" y="213" fill="#c4b5fd" font-size="9" text-anchor="start">{escape(t["n"][1])}</text>')
    o.append(f'<text x="30" y="368" fill="#94a3b8" font-size="10">{escape(t["pin"])}</text>')
    o.append(box(30, 388, 880, 56, "accent", t["hist"], t["hist2"], size=11.5))
    o.append("</svg>")
    return "\n".join(o)


# ── 3. automatic messages ──────────────────────────────────────────────────

AUTO = {
    "en": dict(
        title="Automatic messages: a rule's life and the guards",
        sub="off by default · nothing is sent until the owner confirms exactly what the preview showed",
        st=[("draft → shadow", "records who WOULD get it", "sends nothing", "neutral"),
            ("preview", "text · scope · limits", "dry run + digest", "accent"),
            ("active", "rule activate N", "--confirm <digest>", "secondary"),
            ("paused / done", "by owner, or by itself", "on trouble", "neutral")],
        edit="any edit → back to shadow", yes="owner says yes",
        gt="Checked before EVERY message", g=[
            "autoresponder.enabled = true", "kill switch: stories.py stop", "scope: contacts by default",
            "never_message list", "no bots, deleted, blocked", "charges Stars → skipped, never paid",
            "1 message per person per rule", "7-day cooldown across rules", "40/day contacts · 10/day all",
            "15/hour · quiet hours 22–09", "PEER_FLOOD → everything stops"],
        pause="someone hid your stories after a message → rule pauses"),
    "ru": dict(
        title="Автосообщения: жизнь правила и предохранители",
        sub="выключено по умолчанию · ничего не уйдёт, пока владелец не подтвердит ровно то, что показал превью",
        st=[("draft → shadow", "пишет, кому БЫ ушло", "ничего не шлёт", "neutral"),
            ("preview", "текст · охват · лимиты", "прогон + дайджест", "accent"),
            ("active", "rule activate N", "--confirm <дайджест>", "secondary"),
            ("paused / done", "владельцем или само", "при проблемах", "neutral")],
        edit="любая правка → снова shadow", yes="владелец сказал «да»",
        gt="Проверяется перед КАЖДЫМ сообщением", g=[
            "autoresponder.enabled = true", "стоп-кран: stories.py stop", "охват: по умолчанию контакты",
            "список never_message", "не боты, не удалённые, не блок", "берёт звёзды → пропуск, не платим",
            "1 сообщение человеку на правило", "7 дней тишины между правилами", "40/день контакты · 10/день все",
            "15/час · тихие часы 22–09", "PEER_FLOOD → стоп всего"],
        pause="человек скрыл ваши сторис после сообщения → правило на паузу"),
}


def autoresponder(lang: str) -> str:
    t = AUTO[lang]
    W, H = 940, 520
    o = head(W, H, t["title"], t["sub"])
    ys = [100, 196, 292, 388]
    o.append(arrow("M 150 162 L 150 194", "accent"))
    o.append(arrow("M 150 258 L 150 290", "secondary", t["yes"], 160, 280, anchor="start"))
    o.append(arrow("M 150 354 L 150 386", "neutral"))
    o.append(arrow("M 270 227 L 310 227 L 310 131 L 272 131", "neutral", t["edit"], 318, 182, dash=True,
                   anchor="start"))
    o.append(arrow("M 270 323 L 520 323", "alert"))
    for y, (name, s1, s2, color) in zip(ys, t["st"]):
        o.append(box(30, y, 240, 62, color, name, s1, s2))
    # guard panel
    f, s = C["alert"]
    o.append(f'<rect x="522" y="100" width="388" height="350" rx="8" fill="{f}" stroke="{s}" stroke-width="1.5"/>')
    o.append(f'<text x="540" y="126" fill="white" font-size="12" font-weight="600">{escape(t["gt"])}</text>')
    for i, g in enumerate(t["g"]):
        y = 154 + i * 26
        o.append(f'<circle cx="546" cy="{y - 4}" r="3" fill="{s}"/>')
        o.append(f'<text x="558" y="{y}" fill="#e2e8f0" font-size="10.5">{escape(g)}</text>')
    o.append(f'<text x="30" y="482" fill="#94a3b8" font-size="10">{escape(t["pause"])}</text>')
    o.append("</svg>")
    return "\n".join(o)


if __name__ == "__main__":
    for lang in ("en", "ru"):
        suffix = "" if lang == "en" else ".ru"
        (OUT / f"architecture{suffix}.svg").write_text(architecture(lang), encoding="utf-8")
        (OUT / f"story-life{suffix}.svg").write_text(flow(lang), encoding="utf-8")
        (OUT / f"autoresponder{suffix}.svg").write_text(autoresponder(lang), encoding="utf-8")
    print("written to", OUT)
