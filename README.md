# telegram-stories-stat

**Who watched your Telegram stories, when, and what to do about it** — a skill for personal AI agents
([Hermes Agent](https://github.com/NousResearch/hermes-agent) first, any agent with a shell works).

A small background service collects every viewer of your stories with the exact time, keeps the whole
history Telegram still holds, and the agent answers with ready tables: one story, any period, your core
audience, the best hour to post. Optionally it sends automatic messages to viewers by rules — only to
contacts by default, with hard limits and a kill switch.

No language model runs in the service. The agent only reads the tables it prints.

```
Story 221 · 05.10.2026 14:01 · 📷 photo
👁 133 · ❤ 7 · 💬 2 · ↪ 0 · vs usual +12%
1h: 41 · 6h: 88 · 24h: 121 · 48h: 133 · new viewers: 3
👥 101 · 📇 4 · · 28 · ⭐ 6
```

| # | Time | After | Name | Username | Reaction | Who |
|---:|---|---:|---|---|:---:|:---:|
| 1 | 14:03 | +2m | Kate Smith | [@kate](https://t.me/kate) | ❤ | ⭐👥 |
| 2 | 14:10 | +9m | Alex | — | | 📇 |
| 3 | 15:22 | +1h21m | Max Lee | [@maxlee](https://t.me/maxlee) | 🔥 | · |

*(Made-up rows. In Telegram this arrives as a native table; usernames are clickable.)*

## Quick start

```bash
# 1. install the skill (Hermes)
hermes skills install itpartypattaya/telegram-stories-stat/skills/telegram-stories
# 2. one command, in a terminal: dependencies, database, config, QR login, service, history import
python3 ~/.hermes/skills/telegram-stories/scripts/install.py
```

Step 2 installs Telethon and `qrcode` if they are missing, creates the database and the config, then shows
a **QR code right in the terminal**: on your phone open Telegram → Settings → Devices → *Link Desktop Device*
and scan it. If two-step verification is on, type the cloud password when asked (hidden). The service
starts, and the installer offers to import all your past stories. No login code has to arrive anywhere —
on a real account Telegram twice said "code sent to the app" and nothing came, which is why QR is the
default. Phone + code is still there: `stories.py login --with-code`.

Installed as a plugin instead (`hermes plugins install itpartypattaya/telegram-stories-stat`), the
scripts live in `~/.hermes/plugins/telegram-stories-stat/skills/telegram-stories/scripts/` — use that
path above. The service unit points at whichever copy ran `install.py`. Any other agent with a shell:
clone the repository anywhere, run the same scripts, and point the agent at `SKILL.md`.

You need Telegram API credentials once (`api_id`, `api_hash` from <https://my.telegram.org> → API
development tools); the login asks for them and stores them in `~/.hermes/.env`. Hermes users who already
have `TG_API_ID`/`TG_API_HASH` there are not asked. Then ask your agent: *"who watched my last story?"*,
*"summary of my stories for September"*, *"who is my core audience?"*.

Check everything any time: `install.py --check`.

## How it works

```
                 Telegram (MTProto)
                        ▲
                        │ 1 connection, ~3–5 requests/min
┌───────────────────────┴───────────────────────────────┐
│ daemon.py — the only long-running process (systemd)   │
│  ├ poller:  counters → viewer lists on growth          │
│  ├ listener: replies to stories, replies to messages   │
│  ├ rules engine → queue → sender (all guards)          │
│  └ reports: pulse 2 h after posting, weekly digest     │
└───────────────────────┬───────────────────────────────┘
                        ▼
        SQLite (WAL) ~/.hermes/data/telegram-stories/stories.db
                        ▲
┌───────────────────────┴───────────────────────────────┐
│ stories.py — CLI the agent runs: tables, reports,      │
│ rules, segments, export. Reads SQLite; no Telegram     │
│ calls for tables.                                      │
└───────────────────────────────────────────────────────┘
Notifications → your agent's Telegram bot (native tables) or your Saved Messages.
```

- Telegram has no "someone viewed your story" event, so the service polls: one request per minute
  returns the counters of all active stories; the viewer list is read only when a counter grows, from
  the newest viewer down to the first one already stored. New stories are checked every 5 minutes,
  profile (pinned) stories every 30 minutes — they keep getting views after 48 hours.
- History import walks the story archive and reads every list Telegram still returns. On a real
  account: 209 stories since July 2023, viewer lists available from late August 2023, about 30,000
  viewer rows, ~25 minutes with the default 2-second pause.

### Processes and resources

| | |
|---|---|
| Long-running processes | **1** (`telegram-stories.service`, a systemd user unit) |
| Memory | **~65–70 MB** resident (measured on a live service: 67.8 MB RSS, 53 MB in its cgroup; Python 3.11 + Telethon 1.44) |
| CPU | about 0.5 s to start, then ~3 ms per Telegram request → around 0.1% of one core |
| Network | 1 TCP connection to Telegram; a few KB per minute |
| Disk | database ~5 MB per 30,000 viewer rows; thumbnails ~20 KB per story (optional) |
| Language model calls | **0** — the service and every script are deterministic |
| Short-lived | the CLI when the agent asks for a table (~1 s); history import once |
| Not used | web servers, open ports, third-party services, telemetry |

## Why a separate Telegram session

`stories.py login` creates a new session named **"Hermes Stories"**. It is deliberate:

1. **Isolated risk.** The service is the only part that may write to people on its own. If Telegram
   ever limits or terminates that session, your phone and your agent's other sessions are untouched.
2. **A kill switch in your pocket.** Telegram → Settings → Devices → "Hermes Stories" → Terminate, and
   the service loses access instantly — no server, no terminal.
3. **No shared-key conflicts.** Two long-running programs on one session key are fragile; the same key
   used from two IP addresses at once (a VPN, a proxy) is revoked by Telegram (`AUTH_KEY_DUPLICATED`) and
   both programs break.
4. **Visibility.** Telegram shows when the session was last active.
5. **A separate secret** you can rotate without touching anything else.

Be clear about one thing: Telegram has **no read-only sessions** — any session is full access to the
account. A separate login isolates, it does not restrict. That is why the session string lives only in
the env file (mode 600) and never in the config, the database or the logs, and why you type the login
code and the cloud password yourself in a terminal: a password typed into a chat with an agent stays in
its history and reaches the model provider.

## Tables and reports

| Ask | The agent runs |
|---|---|
| one story, every viewer | `stories.py table story last` (or an id, or `-2`) |
| all stories for a period | `stories.py table summary --period 30d` / `--from … --to …` |
| audience: core, regular, new, cooling, lost | `stories.py table people [--status core]` |
| when people watch, best hour to post | `stories.py table hours --period 90d` |
| compare stories | `stories.py table compare 219 220 221` |
| channel stories | `stories.py table channel --peer @channel` |
| CSV for a spreadsheet | `stories.py export views --period 90d` |

Every person is stored with first name, last name, username (all usernames) and Telegram id; renames are
kept in a history table. In tables the username is a clickable link. People without a username are shown
by name only: Telegram turns `tg://user?id=` links in a bot's message into plain text (checked on a live
rich message), so a link there would only look clickable. CSV exports always carry the id.

**Native tables in Telegram.** Since Bot API 10.1 Telegram renders Markdown tables natively ("rich
messages", up to 32,768 characters). In Hermes switch it on:
`platforms.telegram.extra.rich_messages: true`. Without it Hermes turns tables into bullet groups —
readable, just not as neat. Tables longer than `tables.max_rows` (150) are cut and the full version is
written to a CSV file.

The service also sends, without any model: a **pulse** 2 hours after you post ("📈 +30% above usual") and
a **weekly digest** (Sunday 20:00 by default) plus a monthly one. Definitions:
[`references/metrics.md`](skills/telegram-stories/references/metrics.md).

## Automatic messages (opt-in)

Off by default (`autoresponder.enabled: false`). A rule is *which stories × who × trigger × action ×
limits*: "everyone who watches my next story", "only @alex", "people who reacted ❤", "my core
audience". A new rule runs in **shadow mode** first — it records who would get the message and sends
nothing; activating it requires the digest printed by `rule preview`, so exactly what you saw is what
runs. Guards no rule can switch off: contacts-only by default (`dialog` and `all` are explicit), never
bots or people who charge Stars for messages, one message per person per rule, a 7-day cooldown, caps
per day and hour, quiet hours, and a full stop on Telegram's anti-spam signal. `stories.py stop` halts
everything at once. Spec and reasoning: [`references/rules.md`](skills/telegram-stories/references/rules.md).

## Telegram's limits

Channel stories never reveal viewers (counts, reactions and public reposts only); without Premium,
viewer lists disappear 24 hours after a story expires; incognito viewers are not listed. Details:
[`references/telegram-limits.md`](skills/telegram-stories/references/telegram-limits.md).

## What is stored and where

| Path | What |
|---|---|
| `~/.hermes/telegram-stories.json` | config (no secrets) |
| `~/.hermes/.env` | `STORIES_SESSION_STRING` (+ API id/hash if they were not there) — mode 600 |
| `~/.hermes/data/telegram-stories/stories.db` | stories, viewers with times and reactions, profiles, rules, sent messages |
| `~/.hermes/data/telegram-stories/thumbs/` | small story previews (`backfill --no-thumbs` to skip) |
| `~/.hermes/data/telegram-stories/exports/` | CSV files you asked for |

This is personal data about the people who watch you. It stays on your machine; nothing is sent anywhere
except to Telegram itself and to your own notification chat. Reply texts are not stored — only the fact
of a reply. Paths are overridable with `HERMES_HOME`, `STORIES_HOME`, `STORIES_CONFIG`.

## Config

`~/.hermes/telegram-stories.json`, created by the installer from
[`examples/config.example.json`](skills/telegram-stories/examples/config.example.json). The usual edits:
`timezone`, `locale` (`en` or `ru` table headers), `channels`, `notify` (`auto` = the agent's bot and
home channel, `saved`, `none`), `digest` time, and everything under `autoresponder`.

## Uninstall

`install.py --uninstall` stops and removes the service (`--purge` also deletes the collected data). Then
terminate "Hermes Stories" in Telegram → Settings → Devices and remove `STORIES_SESSION_STRING` from
the env file.

## Threat model, honestly

An agent with a shell can technically do anything the service can. The guards here protect against
mistakes and against instructions hidden in text the agent reads — a rule changes only through a preview
the owner saw — not against a malicious agent. The last lines of defence are the caps, the
contacts-only default, the kill switch and the separate session you can terminate from your phone.

## Disclosure

- **Network:** Telegram MTProto (the stories session); Telegram Bot API `api.telegram.org` for
  notifications when a bot token is configured. Nothing else.
- **Files written:** the paths in the table above; a systemd user unit
  `~/.config/systemd/user/telegram-stories.service`; the env file (one or three keys).
- **Processes:** one long-running service; the CLI on demand; `install.py` runs
  `pip install "telethon>=1.36,<2" "qrcode>=7,<9"` only when they are missing (`--no-deps` to skip).
- **Capabilities:** reads your stories, their viewers, reactions and replies; sends private messages
  only through active rules within the limits above; never posts, never joins chats, never pays.
- **LLM:** none.
- **Updates:** none automatic.

## Tests

`python -m unittest discover -s tests` — synthetic data only, no network, no Telethon needed.

## License

MIT © Anton Vaskov
