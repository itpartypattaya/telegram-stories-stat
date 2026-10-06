---
name: telegram-stories
description: "Telegram stories: who viewed, stats tables, auto-replies."
version: 1.5.0
author: "Anton Vaskov (itpartypattaya), https://t.me/passone"
license: MIT
compatibility: Hermes Agent >= 0.21 (written against 0.21.5); Python 3.10+; Telethon 1.40+ (tested 1.44)
platforms: [linux]
allowed-tools: terminal read_file
tags: [telegram, stories, analytics, telethon, autoresponder]
---

# Telegram Stories Skill

Collects who viewed the owner's Telegram stories and when (every minute while a rule is active, hourly
otherwise, plus the whole history Telegram still keeps), answers with ready statistics tables, and can send opt-in automatic messages to
viewers by rules. A background service does the collecting and sending; it never calls a language model.
It does not post stories, does not read anyone's chats and cannot show who viewed a channel's stories
(Telegram does not reveal that to anyone).

## When to Use

- "Who watched my last story?", "who viewed story 221 and when?", "did @someone see it?"
- "Summary of my stories for September / the last 30 days", "which stories did best?"
- "Who is my core audience?", "who stopped watching?", "when is the best time to post?"
- "Message everyone who watches my next story", "write to @someone when they view it",
  "notify me when X views", "stop the autoresponder".
- Channel stories: views, reactions and reposts per story.

## Prerequisites

- Installed once by the owner **in a terminal**: `python3 "${HERMES_SKILL_DIR}/scripts/install.py"` —
  installs Telethon and qrcode, creates the database and config, shows a QR code to scan with the phone
  (separate Telegram session "Hermes Stories"), starts the service and offers the history import.
  Check with `install.py --check`. If you are asked to set it up, give the owner that command; do not run
  the login yourself.
- Without Telegram Premium, viewer lists vanish 24 h after a story expires — only live collection works.
- Tables render as real tables in Telegram when Hermes has
  `platforms.telegram.extra.rich_messages: true`; otherwise they arrive as bullet groups.

## How to Run

All commands: `python3 "${HERMES_SKILL_DIR}/scripts/stories.py" <command>` via `terminal`.
Table and report commands read the local database; tables and the dashboard first re-read from Telegram
what they show when it is older than 5 minutes — the active stories, an older story asked for by id, a
channel (a few seconds; `--no-sync` skips it). Commands that talk to Telegram re-run themselves with the
interpreter that has Telethon.

## Quick Reference

| Need | Command |
|---|---|
| One story: header + every viewer | `table story last` · `table story 221` · `table story -2` |
| All stories for a period | `table summary --period 30d` · `--from 2026-09-01 --to 2026-09-30` |
| Audience statuses | `table people` · `table people --status core` (core, regular, new, occasional, cooling, lost) |
| When people watch | `table hours --period 90d` |
| Compare stories | `table compare 219 220 221` |
| Channel stories | `table channel --peer @channel` |
| Pulse / digest text | `report pulse last` · `report digest --period 7d` |
| Full CSV | `export views --period 90d` · `export people` · `export stories` |
| Dashboard: one HTML page, charts, viewer lists | `dashboard` · `dashboard --period 1y` (tab open first) |
| Dashboard to show others: no viewer data in the file | `dashboard --anonymized` |
| Fresh data now | `sync` (tables and the dashboard do it themselves when the data is old) |
| Health | `doctor` · `status` |
| Stop all automatic messages | `stop` (resume: `start`) |
| Rules | `rule template` · `rule create --json '…'` · `rule preview N` · `rule activate N --confirm D` · `rule show N` · `rule pause N` |
| Segments | `segment create vip @a @b` · `segment create close --kind close_friends` · `segment list` |
| Members of a group | `segment create vibe --kind chat --source @group` (or a t.me link, or -100… id) · `segment refresh vibe` |

Add `--format csv|json|text` to any table when Markdown is not wanted.

## Procedure

**Statistics questions.**
1. Pick the table from the Quick Reference; run it.
2. Put the Markdown output into the reply **as is** — it is a native Telegram table with clickable
   usernames. Add at most two sentences of interpretation. Never retype, round or invent numbers.
3. If the output ends with `CSV: <path>`, the table was cut: attach that file (in Hermes: a line
   `MEDIA:<path>`) and say so.
4. Keep the italic quality line (`data as of …`, `N of M in the list`, the final read after expiry) — it
   tells how complete the numbers are. If the command printed `note: could not refresh from Telegram`,
   say that the data is as of that time.
5. Unsure which story the owner means: `table summary --period 7d` first, then `table story <id>`.

**Dashboard.** "Make a dashboard", "show it all on one page", "charts of my stories".
1. Run `dashboard`; it prints the path of one HTML file (~10 s on 30,000 views).
2. Send the file to the owner (in Hermes: a line `MEDIA:<path>`) and say it opens in any browser, offline.
3. The file holds viewers' names: send it only to the owner, never post or publish it.
4. To show the statistics to someone else: `dashboard --anonymized` — no viewer names, usernames or ids in
   the file (the owner's captions and previews stay). Share even that copy only when the owner asks.

**Automatic messages (rules).** See `references/rules.md` for the spec and ready recipes (lead magnet at
the end of a series, "reply to get it", "tell me when @someone opens it", warm-up segments).
1. Build the spec from the request (`rule template` shows the shape). Scope defaults to `contacts`;
   use `dialog` or `all` only when the owner asked for it in so many words. Rules that only notify the
   owner (`action: notify`) or fill a segment write to nobody and work with the autoresponder off.
   "Only members of chat X" → a chat segment and `audience: {mode: segment, segment: …}`. Never a hand-made
   static list for that: it does not follow people who join or leave, and only a chat segment is checked
   with Telegram again right before each message.
2. `rule create --json '…'` — the rule starts in shadow mode (records who would get it, sends nothing).
3. `rule preview N` and show the owner the preview **verbatim**, including the text, the scope, the limits
   and the "would send to" count.
4. Only after the owner's explicit yes to that preview: `rule activate N --confirm <digest from preview>`.
   Any edit (`rule update`) puts the rule back in shadow — preview and ask again.
5. Direct messages also need `autoresponder.enabled: true` in the config. Change it only when the owner
   explicitly asks; never on your own initiative.
6. "Stop", "turn it off", "enough" → `stories.py stop` immediately, then confirm.

**Login.** Never ask for the login code or the cloud password in chat. The owner runs
`stories.py login` in a terminal (QR by default, `--with-code` for phone + code); you only tell them the
command.

## Pitfalls

- Channel stories have no viewer list — say so instead of guessing; show counts, reactions, reposts.
- People in incognito mode (a Premium feature) and deleted accounts are not listed; `views` can exceed the
  listed viewers. The gap is not "hidden viewers" by itself — do not name it so.
- "New viewers" means first seen in the collected history (the summary names its start), not first ever.
- "Cooling" and "lost" need stories the person missed: after a pause in posting nobody is lost, and the
  summary shows "—" for "stopped viewing". Say so instead of calling the audience gone.
- `rule show`: "replied to it" is a reply to that very message; "wrote within 7 days after it" is any
  later message and may be about something else. Do not report the second as a result of the rule.
- The best hours compare listed viewers in each story's first 24 hours; with n of 3–5 stories present them
  as an idea to test.
- "Who" icons: 👥 mutual contact, 📇 contact, ⭐ close friend, · not a contact. Usernames are links; people
  without a username appear by name only (bots cannot link to a user by id).
- Names of the owner's contacts come as saved in the owner's address book.
- A story id is per account; `last` and `-N` are relative to the newest story.
- `rule activate` fails if the digest does not match — that is the point: re-preview, re-ask.
- The service is the only writer of automatic messages; do not send them with other tools.

## Verification

- `stories.py doctor` → `ok` for database, session, service, last poll (under ~10 minutes with an active
  rule, under ~70 without: the service then checks hourly; `status` names the pace).
- `stories.py table story last` lists viewers; the header's 👁 is Telegram's view counter, and the quality
  line under it says how many of them are in the list.
- After `rule create`: `rule show N` lists `shadow` deliveries, `status` shows 0 messages sent.
