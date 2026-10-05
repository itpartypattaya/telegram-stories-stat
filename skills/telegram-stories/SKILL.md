---
name: telegram-stories
description: "Telegram stories: who viewed, stats tables, auto-replies."
version: 1.1.0
author: "Anton Vaskov (itpartypattaya), https://t.me/passone"
license: MIT
compatibility: Hermes Agent >= 0.21 (written against 0.21.5); Python 3.10+; Telethon 1.40+ (tested 1.44)
platforms: [linux]
allowed-tools: terminal read_file
tags: [telegram, stories, analytics, telethon, autoresponder]
---

# Telegram Stories Skill

Collects who viewed the owner's Telegram stories and when (live, every minute, plus the whole history
Telegram still keeps), answers with ready statistics tables, and can send opt-in automatic messages to
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
Table and report commands only read the local database (fast, no Telegram calls). Commands that talk to
Telegram re-run themselves with the interpreter that has Telethon.

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
| Fresh data now | `sync` (the service does it every minute anyway) |
| Health | `doctor` · `status` |
| Stop all automatic messages | `stop` (resume: `start`) |
| Rules | `rule template` · `rule create --json '…'` · `rule preview N` · `rule activate N --confirm D` · `rule show N` · `rule pause N` |
| Segments | `segment create vip @a @b` · `segment create close --kind close_friends` · `segment list` |

Add `--format csv|json|text` to any table when Markdown is not wanted.

## Procedure

**Statistics questions.**
1. Pick the table from the Quick Reference; run it.
2. Put the Markdown output into the reply **as is** — it is a native Telegram table with clickable
   usernames. Add at most two sentences of interpretation. Never retype, round or invent numbers.
3. If the output ends with `CSV: <path>`, the table was cut: attach that file (in Hermes: a line
   `MEDIA:<path>`) and say so.
4. Unsure which story the owner means: `table summary --period 7d` first, then `table story <id>`.

**Automatic messages (rules).** See `references/rules.md` for the spec.
1. Build the spec from the request (`rule template` shows the shape). Scope defaults to `contacts`;
   use `dialog` or `all` only when the owner asked for it in so many words.
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
- People in incognito mode (a Premium feature) are not listed; `views` can exceed listed viewers.
- "Who" icons: 👥 mutual contact, 📇 contact, ⭐ close friend, · not a contact. Usernames are links; people
  without a username appear by name only (bots cannot link to a user by id).
- Names of the owner's contacts come as saved in the owner's address book.
- A story id is per account; `last` and `-N` are relative to the newest story.
- `rule activate` fails if the digest does not match — that is the point: re-preview, re-ask.
- The service is the only writer of automatic messages; do not send them with other tools.

## Verification

- `stories.py doctor` → `ok` for database, session, service, last poll under 5 minutes.
- `stories.py table story last` lists viewers; the header's 👁 equals Telegram's view count
  (minus incognito viewers).
- After `rule create`: `rule show N` lists `shadow` deliveries, `status` shows 0 messages sent.
