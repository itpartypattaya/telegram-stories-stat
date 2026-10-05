# What Telegram gives, and what it does not

Checked against Telegram's MTProto API (layer 227, Telethon 1.44) on a real Premium account.

| Question | Answer |
|---|---|
| Who viewed my story and when? | Yes — `stories.getStoryViewsList`: user, date, reaction, newest first, 100 per page. |
| How long does the list live? | With Premium: permanently (lists from the first weeks of the stories feature in 2023 may be missing). Without Premium: until 24 h after the story expires (`story_viewers_expire_period`). |
| Is there a push event "someone viewed"? | No. The service polls the counters every minute and reads the list only when they grow. |
| Re-views? | Telegram returns one row per viewer; if its date moves, both the first and the latest date are kept. |
| Incognito viewers? | Premium users can view in incognito mode; they are not in the list. The counter may include them. |
| Who reacted? | Each viewer row carries the reaction; counters per emoji come with the story. |
| Replies to a story? | They are private messages with a story reply header; the service counts them as they arrive (no text stored). |
| Channel stories: viewers? | No — Telegram does not reveal channel story viewers to anyone. Available: counters, a views-over-time graph (channel statistics), reactions and public reposts with names. Admin rights to the channel's stories are required. |
| Posting stories from a channel | Needs channel boosts (a channel at boost level 0 cannot post). |
| Paid messages | A user can charge Stars for messages from non-contacts (`send_paid_messages_stars`). The service skips such people. |
| Account restrictions | Too many unanswered first messages or reports → `PEER_FLOOD`; the account may be limited to mutual contacts. The service stops all sending on the first `PEER_FLOOD`. |
| Sessions | Any session is full access to the account; there are no read-only sessions. The service uses its own named session so it can be terminated alone. |

Request budget of the running service: about 3–5 requests a minute (one counters request for all active
stories, a viewer-list page only on growth, new stories every 5 minutes, profile stories every 30
minutes, channels every 15 minutes). History import: about two requests per story with a 2-second
pause — roughly 15 minutes for 200 stories.
