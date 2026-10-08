# Metrics — exact definitions

All times are stored in UTC and shown in the config `timezone`. A story deleted in Telegram is marked
`deleted` and is left out of every list of stories, median and comparison; its viewer rows stay, so the
people who viewed it keep that activity.
 "Listed viewers" are the rows Telegram
returned in the viewer list; "views" is Telegram's own counter (it also counts incognito viewers and
deleted accounts, so it can be a little higher).

## One story (`table story`)

| Metric | Definition |
|---|---|
| 👁 views | Telegram's counter for the story. |
| ❤ reactions | Telegram's reaction counter (each viewer's reaction is also in the viewer row). |
| 💬 replies | Private messages that reply to this story (counted from the moment the service runs). |
| ↪ forwards | Public forwards and reposts Telegram reports. |
| 1h / 6h / 24h / 48h | Listed viewers whose first view came within N hours of posting. |
| Time | First time Telegram reported this viewer (later re-views are kept in `view_events`). |
| After | Time between posting and that first view. |
| vs usual | Finished story: views ÷ median views of the previous 20 stories, as ±%. A story younger than 48 h (marked ⏳) is still collecting views, so it is compared fairly: listed viewers so far ÷ median of the previous 20 stories at the same age (only stories whose list covers 80%+ of the counter — a partial old list says nothing about its first hours; the pulse uses the same baseline). |
| new viewers | Listed viewers with no earlier view in the collected history (not "first ever": the history starts with the first story that has a viewer list, and the summary names that date). |
| 👥 / 📇 / · / ⭐ | Mutual contact / contact / not a contact / on the owner's Close Friends list. |
| quality line | `data as of` — when this story was last read from Telegram; `N of M in the list` — listed viewers against the counter (the gap is incognito viewers, deleted accounts, or a list Telegram stopped giving — not a measured "hidden audience"); the final read — the full list read once more after the story expired (`stories.finalized_at`), pending, or never made (older than the window). |

## Period summary (`table summary`)

| Metric | Definition |
|---|---|
| Stories | Stories posted in the period. |
| Median | Median of their view counters. |
| 1st hour | Share of the story's listed viewers who came in the first hour. |
| Unique viewers | Distinct people with a first view inside the period. |
| New viewers | People whose first view in the collected history falls inside the period. |
| Stopped viewing | People with 2+ views in the 30 days before the period and none inside it. Shown only when the period has 2+ stories with a viewer list; otherwise "—" (unknown, not zero): with nothing posted nobody could stop. |
| Quality line | Time of the last poll, stories without a viewer list, stories waiting for the final read, and the start of the collected history. |
| Active audience (30 d) | Distinct viewers in the last 30 days of the period. |

## Audience statuses (`table people`)

Computed over the last 20 stories that have a viewer list ("seen x/y" counts only stories posted since
the person's first view, minus two days of slack).

| Status | Rule (first match wins) |
|---|---|
| new | First view within the last 14 days. |
| lost | No view for 60+ days **and** 3+ stories missed. |
| cooling | No view for 21+ days **and** 2+ stories missed, but used to see 40%+ of stories. |
| core | Saw 70%+ of eligible recent stories and typically within 3 hours. |
| regular | Saw 40%+. |
| occasional | Everyone else. |

"Typical lag" is the median time from posting to the person's first view over those stories. "Missed" are
stories with a viewer list, posted after the person's last view and at least a day old: time alone does not
make anyone lost — during a pause in posting nobody misses anything.

## When people watch (`table hours`)

Views per local hour and weekday (first views). "Best hours to post" ranks posting hours by the listed
viewers each story collected **in its first 24 hours** — the same window for every story, so old stories
with a long tail and young ones still collecting are compared fairly. Only stories at least a day old whose
viewer list covers 80%+ of the counter (the oldest imported stories have partial lists with views dated weeks
later — their first day cannot be read); only hours with 3+ stories. Each hour shows the median, the range (lowest–highest) and the
number of stories: with a handful of stories it is a hypothesis to test, not a rule.

## Dashboard (`dashboard`)

The page uses the definitions above; these are the additions.

| Block | Definition |
|---|---|
| Period tabs | Last 30 days, 90 days, 365 days, all time. "vs the previous period" compares with the period of the same length right before it; under 5% change is shown without color. |
| Views per story | Telegram's counter of each story in the period. Color: "vs usual" ≥ +10% green, ≤ −10% red; a story younger than 48 h is pale. Dashed line: median of the bars. |
| Best stories | Up to six finished stories with the highest "vs usual" (the block needs at least three). Thumbnails come only for them. |
| How fast views come | Finished stories whose viewer list covers 80%+ of the counter: per story, the share of listed viewers who came within 1 / 6 / 24 / 48 h; the median over stories. "Half of the viewers come within": median of each story's median lag. |
| When people watch | First views by weekday × local hour; cell color relative to the busiest cell. |
| Month by month | Calendar months, local time. Bar: people with a first view of any story that month. Dark part: people whose first view ever was that month. Line: median view counter of stories posted that month. |
| Audience | The statuses and the table of `table people`. |
| Anonymized copy | `dashboard --anonymized`: the same numbers and charts and the status groups as counts. The table of people, the viewer lists and the autoresponder rules are left out; the data inside the file has no people and no viewer rows, so no name, username or id of a viewer is in it. The owner's captions and story previews stay. |
| Open the story | `stories.link` = `https://t.me/<username>/s/<id>` (no username — no link). Telegram opens it while the story is available to whoever opens it. |

## Pulse and digests (sent by the service)

- Pulse: N hours after posting (default 2), listed viewers so far vs the median of the previous 20
  stories at the same age; 📈 at +10% or more, 📉 at −10% or less.
- Weekly digest on the configured weekday and time, monthly digest on the configured day: period summary
  (top 10 by views), new core members, people cooling down, best hours to post.
