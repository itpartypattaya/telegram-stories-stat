# Changelog

**English** · [Русский](CHANGELOG.ru.md)

All notable changes of the skill. Versions follow `plugin.json` and `SKILL.md`. Newest first.

## 1.5.0 — 2026-10-06

Findings of an independent review of 1.4.0.

### Fixed

- **The preview of a `reply` rule counts replies.** Before, it counted the views: a person who only viewed the
  story was in "would send to". Now the preview takes the events of the rule's trigger (replies, reactions or
  views), counts a reply even if its view was never listed, and says how many people are outside the audience.
- **The last views of a story are not lost.** After a story expires, the service reads its viewer list one last
  time (within 7 days, `poll.finalize_window_s`; without Premium within 20 hours, while Telegram keeps the list).
  Before, the views between the last poll and the expiry (up to an hour at the hourly pace) were never read. A
  failed read is tried again in 30 minutes; channels get their final counters.
- A list that Telegram stopped giving (24 hours after the expiry without Premium) no longer marks the story as
  "no viewer list": the viewers already stored stay in comparisons.
- **A table refreshes what it shows.** `table story <id>` of an older story reads that story (and fetches it if
  it is not in the database); `table channel --peer @x` reads that channel. If the refresh fails, the note names
  the time of that data.
- **"Lost" and "cooling" need missed stories.** Before, 60 (21) days without a view were enough, so a pause in
  your posting made the audience "lost". Now "lost" also needs 3 missed stories, "cooling" 2. "Stopped viewing"
  in a summary is "—" when the period has fewer than 2 stories with a viewer list.
- **Replies to automatic messages are linked honestly.** "Replied to it" is a reply to that very message.
  Any other message counts only as "wrote within 7 days after it", and only for the latest automatic message.
  Before, any message within 30 days marked every message to that person as replied.

### Changed

- **Best hours to post** compare the viewers of each story in its first 24 hours, not the final counters, and
  show the range and the number of stories.
- "New viewers" are named for what they are: first seen in the collected history, and the summary names the
  date when that history starts.
- Story and summary tables end with a quality line: when the data was read, how many viewers of the counter are
  in the list, the final read after expiry, stories without a list.
- README, SKILL.md and the references describe the hourly pace, the counter and the list the same way.

### Added

- **`stories.py dashboard --anonymized`**: a copy of the dashboard to show others (`dashboard-anon.html`). All
  numbers, charts and audience groups; no viewer names, usernames or ids in the page or in its data. The table
  of people, the viewer lists and the rules are left out; your captions and previews stay.

### Removed

- `stories.py login start` / `login finish` — the two-step login "for agents". A login code must not pass
  through a chat; log in with `stories.py login` in a terminal (QR, or `--with-code`).

### Upgrade

- The database changes to schema v4 at the first start (two new columns). Stories whose list was already read
  after the expiry count as finalized; replies recorded before count as "wrote after".

## 1.4.0 — 2026-10-06

### Changed

- **The service checks hourly when no rule is active.** With at least one active rule (a message, a
  notification, a segment that fills itself), the service checks each minute as before. Activating a rule brings
  the minute pace back in less than a minute. `poll.idle_s` sets the slow interval (3600 by default); `0` keeps
  the minute pace always.
- The pulse reads its story again right before it goes, and a summary reads the active stories, so the numbers
  are fresh at the hourly pace too.
- Tables and the dashboard read Telegram first if the data is older than 5 minutes. `--no-sync` skips this.
- `stories.py sync` (and the refresh before a table) sends the new views through the rules, as the service does.
  Before, a manual pass could hide new viewers from an active rule.
- The service writes an "alive" mark each minute (`state.alive_at`), apart from the time of the last poll, for
  watchdogs. `doctor` and `status` name the current pace and accept an older last poll at the hourly pace.

## 1.3.0 — 2026-10-06

### Added

- **Group segments: "only the members of this group".** `stories.py segment create vibe --kind chat --source @group`
  (or a `t.me` link, or the numeric ID `-100…`). The service reads the member list of the group into the segment
  and reads it again each 15 minutes (`poll.chat_segments_s`). A rule with this segment as its audience writes
  only to members of the group.
- **A check right before each message.** For a group segment, the service asks Telegram if the person is a member
  of the group now. If the person left the group, the message does not go (`not_in_chat`). If Telegram gives no
  clear answer, the message does not go either (`chat_unverified`).
- The rule preview names the group, the number of members and the time of the member list. If the last read of
  the list failed, the preview shows the error. A rule that targets a segment that does not exist says so.
- `stories.py segment refresh [name]` reads the member list again now.
- **A direct link to each story** in the database: `stories.link` = `https://t.me/<username>/s/<id>`. New stories
  get it when they are stored. When the account or a channel changes its username, the service makes the links
  again. Without a username there is no public link.
- **Dashboard: the button "Open the story"** on the cards of the best stories and in the story window. Story
  numbers in the dashboard tables are links too.
- The story table in Telegram and the CSV exports of stories contain the link.

### Changed

- The dashboard template, opened as is, says that it is a template and tells where the dashboard is.

### Upgrade

- The database changes to schema v3 at the first start (one new column). The service fills the links of the
  stories that are already in the database.

## 1.2.0 — 2026-10-06

### Added

- **One-file HTML dashboard:** `stories.py dashboard`. Period tabs (30 days, 90 days, 1 year, all time) with the
  change against the previous period; views per story; the best stories with previews; how fast a story collects
  its viewers; the best hours to post; a map of views by day of the week and hour; the trend by month; audience
  groups and a table of all viewers with a search; channel stories; autoresponder rules. Select a story to see its
  viewers; select a person to see the stories that this person viewed.
- The page makes no network requests (Content-Security-Policy `default-src 'none'`). The file is readable only by
  its owner (mode 600).
- The service downloads the previews of new stories (before, only the history import did).

## 1.1.2 — 2026-10-06

### Changed

- README in English and Russian rewritten in simple language, with examples of the autoresponder.

### Fixed

- A rule can name an @username that has no views yet. The rule matches the person at the first view.
- A notification to the owner names the time of the view or the reply.
- A reply to a story from a first-time viewer is not lost: the service stores the profile before the rules run.
- A segment that a rule fills is visible to the next rule without a manual `segment create`.

## 1.1.1 — 2026-10-06

### Fixed

- CSV with the delimiter `;` writes decimals with a comma, as spreadsheets in these locales expect.

## 1.1.0 — 2026-10-06

### Added

- Login by QR code by default (`--with-code` for phone and code). `install.py` installs Telethon and qrcode and
  starts the login, the service and the history import in one command.

### Fixed

Findings of an independent review:

- A message is claimed right before sending, in one write transaction that checks the stop switch, the rule status
  and its confirmation code again. A stop, a pause or a change of the rule during the network checks stops the
  message.
- Activation checks the confirmation code inside the write. Each queued message keeps the code of its rule; a
  change of the rule cancels the queue. "The next story" binds only after activation.
- The config is read again before each round of sending. A fresh profile is read before each message.
- A send with an unclear result is never repeated. Messages left in "sending" after a crash become "failed".
- Daily limits count the scope recorded at sending. `never_message` matches all active usernames. A data folder
  belongs to one account only.
- Growth of a story is measured against the counters at the last read of the viewer list (schema v2), so no viewer
  is missed. A full read each 30 minutes catches changed reactions. Pinned stories are read page by page.
- Incomplete profiles do not erase known flags (for example, paid messages).

### Upgrade

- The database changes to schema v2 at the first start.

## 1.0.0 — 2026-10-06

### Added

- A background service (one process, no language model) that collects who viewed your stories and when, and
  imports the history that Telegram still keeps.
- Statistics tables in Markdown that Telegram shows as real tables: one story, a period summary, audience groups,
  hours, a comparison, channel stories. CSV export.
- A pulse two hours after posting and weekly / monthly summaries.
- Opt-in automatic messages by rules: test mode, confirmation of the exact rule, contacts only by default, limits,
  quiet hours, a stop switch.

### Fixed (after the first release)

- A story younger than 48 hours is compared with earlier stories at the same age, not with finished stories.
- A caption is never cut inside a link or a username.
