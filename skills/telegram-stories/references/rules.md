# Automatic messages — rule spec and guards

## Spec (JSON for `rule create --json` / `rule update N --json`)

```json
{
  "name": "Thanks for watching the launch",
  "stories":  {"mode": "next"},
  "audience": {"mode": "all"},
  "scope":    "contacts",
  "trigger":  "view",
  "action":   {"type": "dm", "text": "Hi {first_name}! Thanks for watching 🙌"},
  "limits":   {"per_day": 20, "total": 100},
  "delay_s":  [120, 360],
  "valid_until": "2026-12-31"
}
```

| Field | Values |
|---|---|
| `stories.mode` | `ids` (+ `"ids": [221, 222]`) · `next` (the next story posted after activation) · `tag` (+ `"tag": "#launch"`, matched in the caption) · `all` |
| `audience.mode` | `all` · `users` (+ `"users": ["@ann", 12345]`) · `segment` (+ `"segment": "vip"`; a chat segment = members of a group) · `reacted` (+ optional `"reactions": ["❤"]`) · `new` (first-ever viewers) · `status` (+ `"status": ["core"]`) |
| `scope` | `contacts` (default — in the owner's contacts) · `dialog` (the person has written to the owner before) · `all` (strangers too) |
| `trigger` | `view` · `reaction` · `reply` (a private reply to the story) |
| `action.type` | `dm` (+ `text` or `variants: [...]` for an A/B split by user id) · `notify` (tell the owner, write to nobody) · `segment` (+ `segment`: add the person to a static segment) |
| `limits` | `per_day`, `total` for this rule |
| `delay_s` | random delay range before sending (minimum 30 s); default from the config |
| `valid_until` | `YYYY-MM-DD`; the rule stops matching after that day |

`{first_name}` in a text is replaced with the recipient's first name. Messages go out as plain text: a link
is pasted as is (`https://…`), Markdown is not parsed, files and buttons cannot be attached.

`audience.users` may name an @username nobody in the database has yet: the rule then matches that person
by username the first time they view (the preview says so). A numeric Telegram id works too.

## Recipes

| Request | Spec |
|---|---|
| Lead magnet for whoever reaches the last story of a series | owner puts a marker such as `#guide` into the **last** story's caption; `stories: {mode: tag, tag: "#guide"}`, `trigger: view`, `action: dm` with the link. Activate **before** posting: people who viewed earlier are not messaged. Viewers who are not contacts need `scope: all` (10/day by default) |
| "Reply to this story and I'll send the guide" | same `tag`, `trigger: reply`, `scope: dialog` — the person has just written to the owner, so this is not a cold message; 40/day by default. Any reply counts: the reply text is not read |
| Tell me when @someone opens a story | `audience: {mode: users, users: ["@someone"]}`, `action: {type: notify}`, `delay_s: [30, 60]`. Works with the autoresponder switched off: it writes to nobody but the owner. The notification names the moment of the view |
| Confirm to @someone in private that they saw a public notice | as above with `action: dm`, `scope: all` (or `contacts` if they are) — pair it with the notify rule to know as well |
| Thank people who reacted | `trigger: reaction`, `audience: {mode: all}` (or `reacted` + `reactions: ["❤"]`), `scope: contacts` |
| Warm-up: mark who watched story A, write to them after story B | rule 1: story A, `action: {type: segment, segment: "warm"}` (writes to nobody); rule 2: story B, `audience: {mode: segment, segment: "warm"}`, `action: dm` |
| Greet first-time viewers yourself | `audience: {mode: new}`, `action: notify` — the owner decides whom to write to |
| Only the members of a group | `segment create vibe --kind chat --source @group` (a t.me link or the numeric id -100… also work; the account must be in the group), then `audience: {mode: segment, segment: "vibe"}`. The service reads the member list every 15 min (`poll.chat_segments_s`); right before each message it asks Telegram whether this person is a member **now** — left, or no clear answer → skipped (`not_in_chat` / `chat_unverified`). Combine with `scope: dialog` for "members who have written to me" |

Timing: active stories are checked every minute and new stories every 5 minutes; then the rule's delay
(default 2–6 min, at least 30 s) and quiet hours apply — to owner notifications as well. Profile (pinned)
stories younger than 30 days are checked every 30 minutes, so a rule on a pinned story keeps working.

## Life cycle

`create` → **shadow** (deliveries are recorded with status `shadow`, nothing is sent) → `preview` prints a
summary, a dry run over the events already collected and a digest → `activate N --confirm <digest>` →
**active**. The dry run takes the events of the rule's trigger — replies for `reply` (a reply counts even if
its view was never listed), views with a reaction for `reaction`, views for `view` — and names how many
people were left out and why (`not_in_audience`, `scope_contacts`, `cooldown`…). "Would send to" passes the
offline checks; limits, quiet hours and the checks right before a message can still hold some back. The digest covers stories, audience, scope, trigger, action and limits; any change resets
the rule to shadow. `pause` cancels queued messages; `delete` marks the rule done and keeps history.

`next` binds to the first story posted **after activation** (a story posted while the rule was in shadow
is never the target). Any update, pause or return to shadow cancels the rule's queued messages, and every
queued message carries the rule's digest — it is sent only if the rule still has exactly that digest.
Changes to `telegram-stories.json` (switching the autoresponder off, `never_message`, limits) reach the
running service without a restart: it re-reads the file before every round of sending.

## Guards no rule can switch off

1. `autoresponder.enabled` must be true in the config for any direct message.
2. Kill switch: `stories.py stop` cancels the queue and blocks sending until `stories.py start`.
3. Never messaged: the owner, `never_message` entries, bots, deleted accounts, people who blocked the
   owner or hid the owner's stories, **people who charge Stars for messages** (Telegram reports this in
   their profile — skipped before any attempt; the service never pays).
4. Scope filter (contacts by default). `dialog` is checked against the real chat before sending.
5. One message per person per rule, ever; no automatic message to a person more often than
   `cooldown_days` (default 7) across all rules; nothing if the owner wrote to them in the last 24 h.
6. Caps per account: 40/day for `contacts` and `dialog`, 10/day for `all`, 15/hour overall.
7. Quiet hours (default 22:00–09:00): sending waits until the morning.
8. Telegram anti-spam: `PEER_FLOOD` stops everything and alerts the owner; a flood wait over 5 minutes
   pauses sending for that long; three failures in a row pause the rule.
9. If a recipient hides the owner's stories or blocks the owner after an automatic message, the rule
   that wrote to them pauses and the owner is told.
10. Right before each message the service fetches the recipient's current profile (contact status, Stars
    price) and claims the message in one database transaction that re-checks the kill switch, the rule's
    status and its digest. `stories.py stop` therefore stops everything except, at most, the one message
    already handed to Telegram.
11. A send whose outcome is unknown (timeout, dropped connection, crash) is recorded as failed and **never
    repeated** — a second copy to a real person is worse than a missing one.
12. A data folder belongs to one account: logging in with another account, or starting the service with a
    session of another account, is refused — rules and queued messages never move between accounts.

## Replies to automatic messages

`rule show` counts two kinds. **Replied to it**: the person answered that very message (a Telegram reply to
it). **Wrote within 7 days after it**: any other private message from the person, linked only to the latest
automatic message they got in the last 7 days — it may be about something else, so do not report it as the
rule's result. Rows from before 1.5.0 were all counted the loose way and show as "after".

## Why these limits

Telegram judges a personal account by how people react to its first messages. Unsolicited messages to
strangers that get reported can restrict the account to writing only to mutual contacts. Keep `all`
for small, explicit cases, write like a person, and prefer `notify` (the owner writes personally) when
in doubt.
