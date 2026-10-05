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
| `audience.mode` | `all` · `users` (+ `"users": ["@ann", 12345]`) · `segment` (+ `"segment": "vip"`) · `reacted` (+ optional `"reactions": ["❤"]`) · `new` (first-ever viewers) · `status` (+ `"status": ["core"]`) |
| `scope` | `contacts` (default — in the owner's contacts) · `dialog` (the person has written to the owner before) · `all` (strangers too) |
| `trigger` | `view` · `reaction` · `reply` (a private reply to the story) |
| `action.type` | `dm` (+ `text` or `variants: [...]` for an A/B split by user id) · `notify` (tell the owner, write to nobody) · `segment` (+ `segment`: add the person to a static segment) |
| `limits` | `per_day`, `total` for this rule |
| `delay_s` | random delay range before sending (minimum 30 s); default from the config |
| `valid_until` | `YYYY-MM-DD`; the rule stops matching after that day |

`{first_name}` in a text is replaced with the recipient's first name.

## Life cycle

`create` → **shadow** (deliveries are recorded with status `shadow`, nothing is sent) → `preview` prints a
summary, a dry run over views already collected and a digest → `activate N --confirm <digest>` →
**active**. The digest covers stories, audience, scope, trigger, action and limits; any change resets
the rule to shadow. `pause` cancels queued messages; `delete` marks the rule done and keeps history.

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

## Why these limits

Telegram judges a personal account by how people react to its first messages. Unsolicited messages to
strangers that get reported can restrict the account to writing only to mutual contacts. Keep `all`
for small, explicit cases, write like a person, and prefer `notify` (the owner writes personally) when
in doubt.
