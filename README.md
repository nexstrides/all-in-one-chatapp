# All-in-One Chat

One inbox for conversations from several chat platforms. Each connected
account (a Slack workspace, a Telegram bot, ...) pushes events to a webhook;
the backend normalizes them into a single model of conversations and
messages, and a client reads and replies through one JSON API. Replies are
queued and delivered back to the platform by a small worker. Accounts that
belong to the same human can be grouped into a *person*, which gives one
unread count and one timeline across platforms.

This repo is the backend only: Flask, SQLite and the standard library.
Requires Python 3.10+ whose SQLite includes FTS5 (python.org, Debian/Ubuntu,
Homebrew and conda builds all do; the app refuses to start otherwise). Flask
is the only third-party dependency.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python run.py                 # http://localhost:5000, creates instance/aiochat.sqlite3
python scripts/seed.py        # optional demo data (Slack + Telegram, one grouped person)
```

Run the tests (no server, external services, flags or environment variables
needed; each test uses its own temporary database):

```bash
python -m unittest discover -s tests -t .
```

No network access? Download the wheels on a machine that has it
(`pip download -r requirements.txt -d wheels`), copy the `wheels/` folder
over, and install with
`pip install --no-index --find-links wheels -r requirements.txt`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AIOCHAT_DATABASE` | `instance/aiochat.sqlite3` | SQLite file path |
| `AIOCHAT_API_TOKEN` | unset | Bearer token for `/api/*` (unset = auth off, dev only) |
| `AIOCHAT_DEFAULT_PAGE_SIZE` | `25` | Default page size for list endpoints |
| `AIOCHAT_MAX_PAGE_SIZE` | `100` | Upper bound on `page_size` |
| `AIOCHAT_OUTBOX_WORKER` | `1` | `run.py` starts an in-process delivery thread |
| `AIOCHAT_OUTBOX_INTERVAL` | `15` | Seconds between delivery passes (retries; new replies go out immediately) |
| `AIOCHAT_OUTBOX_MAX_ATTEMPTS` | `5` | Give up on a reply after this many transient failures |
| `AIOCHAT_DELETED_RETENTION_DAYS` | `30` | Keep messages deleted on the platform (hidden) for this long; `0` = forever |
| `AIOCHAT_HTTP_TIMEOUT` | `10` | Seconds to wait on platform API calls |

## Connecting a platform

Create a channel, then point the platform at its webhook URL. The response
to `POST /api/channels` contains `webhook_token`, shown only once. The
`credential` is the platform's bot token; without it a channel is
receive-only and replies wait in the outbox until one is set (`PATCH`).

### Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Create the channel:
   ```bash
   curl -X POST localhost:5000/api/channels -H 'Content-Type: application/json' \
     -d '{"platform":"telegram","name":"My bot","external_account_id":"<bot id>","credential":"<bot token>"}'
   ```
3. Register the webhook, passing the channel's `webhook_token` as Telegram's
   secret (it arrives in the `X-Telegram-Bot-Api-Secret-Token` header):
   ```bash
   curl "https://api.telegram.org/bot<bot token>/setWebhook" \
     -d "url=https://<your host>/api/webhooks/telegram/<channel id>" \
     -d "secret_token=<webhook_token>"
   ```

### Slack

1. Create a Slack app, add the bot scopes `chat:write`, `channels:history`,
   `groups:history`, `im:history`, install it and copy the bot token (`xoxb-...`).
2. Create the channel with `"platform": "slack"`, the team id as
   `external_account_id` and the bot token as `credential`.
3. Under *Event Subscriptions*, set the request URL to
   `https://<your host>/api/webhooks/slack/<channel id>?token=<webhook_token>`
   (Slack cannot send custom headers, so the token travels in the URL) and
   subscribe to `message.channels`, `message.groups` and `message.im`.
   The app answers Slack's `url_verification` handshake automatically.
4. Invite the bot to the channels you want in the inbox.

The webhook must be reachable from the internet; for local development use a
tunnel such as ngrok or cloudflared.

## Delivery

Replies are stored immediately and queued in `outbox`. `run.py` runs a
delivery thread in the server process: a reply wakes it up, and it also polls
every `AIOCHAT_OUTBOX_INTERVAL` seconds for retries. Transient failures
(network, rate limits, 5xx) back off 30 s, 2 min, 10 min, 30 min and give up
after `AIOCHAT_OUTBOX_MAX_ATTEMPTS`; permanent ones (bad token, unknown
chat) fail at once. Each message reports `delivery_status` and
`delivery_error`, and failed ones can be re-queued.

With a multi-process server (gunicorn etc.) set `AIOCHAT_OUTBOX_WORKER=0` and
run one dedicated worker instead:

```bash
flask --app run deliver-outbox --loop      # or without --loop, from cron
```

Workers claim queue rows atomically, so running several is safe, just
redundant.

## Edits and deletions

Platforms report changes to messages after the fact; the inbox applies them
to the stored copy rather than creating new rows:

- **Edits** (Slack `message_changed`, Telegram `edited_message`) replace
  `body` and set `edited_at`. Read state is kept, the unread count does not
  change, and the conversation does not move up the list. The preview
  reflects the new text if the edited message is the latest one. A
  `message_changed` whose text is unchanged (Slack sends these for link
  unfurls) is a no-op.
- **Deletions** (Slack `message_deleted`, or a `tombstone` for a thread
  parent) are soft: the row gets `deleted_at` and disappears from message
  lists, previews, unread counts and search. `last_message_at` falls back
  to the newest remaining message, so the conversation reorders as it would
  on the platform. Pass `include_deleted=1` to see deleted messages in
  place. History is kept for `AIOCHAT_DELETED_RETENTION_DAYS`, then
  removed by the worker (or `flask --app run purge-deleted`). Keeping the
  row also means a late retry of the original event cannot resurrect it.
- Telegram does not notify bots of deletions, so deletes only ever come
  from Slack.

Edits and deletions of messages the inbox never stored are counted as
`unmatched` in the webhook response and otherwise ignored.

## People

A *contact* is one account on one channel: a Slack user in a workspace, a
Telegram user as seen by a bot. The same human therefore shows up once per
platform. A *person* is a group of contacts you create by hand to say "these
are all the same human"; nothing is grouped automatically (see
[Suggesting matches](#suggesting-matches) for why).

```bash
# find the accounts
curl 'localhost:5000/api/contacts?q=mia'
# group them
curl -X POST localhost:5000/api/people -H 'Content-Type: application/json' \
  -d '{"name": "Mia Park", "contact_ids": [3, 7]}'
```

A contact belongs to at most one person. Linking a contact that is already
on another person is refused (`409 contact_linked`) rather than silently
moved; unlink it first. Deleting a person only ungroups its contacts; their
messages are untouched. Deleting a channel removes its contacts from any
person they were in.

Grouping gives you, per person:

- `unread_count`: unread inbound messages written by any of the person's
  contacts, on every platform. Marking a conversation read lowers it as
  usual; your own replies and deleted messages never count.
- `GET /api/conversations?person_id=`: the conversations in which the
  person has written, including shared channels. Combines with
  `channel_id` and `unread=1`.
- `GET /api/people/<id>/timeline`: one paginated list of everything the
  person wrote, on any platform, newest first. Each message carries its
  `conversation: {id, title, platform}` so a client can jump to context.
  Messages by other members of a shared channel and your own replies are
  not included. `include_deleted=1` shows deleted messages in place.
- `person_id` as a search filter (below).

Existing databases are upgraded in place on startup: the `people` table and
the `contacts.person_id` column are added and every contact starts
ungrouped.

## Search

`GET /api/messages?q=` is a full-text search over every non-deleted message,
newest first, backed by an SQLite FTS5 index that is kept in step with the
`messages` table by triggers. It matches **words**, not substrings: every
word of the query must occur in the message, in any order, so
`notes standup` finds "Can you send me the standup notes?". Case and accents
are folded (`cafe` finds `café`), and punctuation is not part of a word
(`c++` is the word `c`; `budget-review` is the two adjacent words
`budget review`). A query with no letters or digits matches nothing.

| `match` | `q=note stand` | `q=budget` matches |
| --- | --- | --- |
| `word` (default) | nothing: neither is a whole word | `budget`, `Budget!`, `(budget)`; not `budgets` |
| `prefix` | "the standup notes" | also `budgets`, `budgeting`; not `rebudget` |

Nothing typed by the user is FTS5 syntax: `OR`, `NOT`, `*`, quotes and
`column:` are ordinary words or ignored. There are no phrase queries or
relevance ranking yet; results are in time order like the rest of the inbox.

Filters, all optional and combinable:

| Parameter | Keeps messages... |
| --- | --- |
| `person_id` | written by that person's contacts (404 if the person does not exist) |
| `platform` | from channels on that platform (`slack`, `telegram`) |
| `since` | sent at or after this time |
| `until` | sent at or before this time |

`since` and `until` take a date (`2025-10-26`) or an ISO 8601 timestamp
(`2025-10-26T17:30:00Z`, `2025-10-26T18:30:00+01:00`; no offset means UTC).
A bare date covers that whole UTC day, so `since=D&until=D` is one day.

Databases from before full-text search get the index built on first start
(about a second per 200k messages). If you ever change `messages` by hand,
`flask --app run reindex-search` rebuilds it.

## Layout

```
aiochat/
  __init__.py        app factory
  config.py          settings from environment variables
  db.py, schema.sql  SQLite connection, schema, column migrations, FTS5 index upkeep
  auth.py            bearer-token check for the client API
  errors.py          ApiError and JSON error responses
  textmatch.py       turns search text into a safe FTS5 MATCH expression
  worker.py          outbox delivery thread, `deliver-outbox` and `purge-deleted` CLI
  connectors/        one module per platform: webhook payload -> InboundMessage,
                     and send() for replies; http.py is the stdlib HTTP client
  services/          channels.py, inbox.py, outbox.py, people.py (all SQL lives here)
  api/               Flask blueprints: channels, contacts, conversations, messages,
                     outbox, people, webhooks
scripts/seed.py      replays sample webhooks to load demo data
tests/               unittest suite
```

Adding a platform means one connector module (`parse`, `send`, optionally
`handshake`) registered in `connectors/__init__.py`; nothing else needs to
know about it.

## API

All `/api/*` routes except webhooks need `Authorization: Bearer <AIOCHAT_API_TOKEN>`
when a token is configured. Errors look like
`{"error": {"code": "not_found", "message": "..."}}`.

### Channels

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/channels` | List connected accounts; `can_send` says whether a credential is set |
| `POST` | `/api/channels` | `{"platform", "name", "external_account_id", "credential"?}`; the response includes `webhook_token`, shown only once |
| `GET` | `/api/channels/<id>` | |
| `PATCH` | `/api/channels/<id>` | `{"name"?, "credential"?}`; `"credential": null` clears it. Credentials are never returned |
| `DELETE` | `/api/channels/<id>` | Also deletes its conversations and messages |

### Webhooks

`POST /api/webhooks/<platform>/<channel_id>` with the channel's token in
`?token=`, `X-Telegram-Bot-Api-Secret-Token` or `X-Webhook-Token`.
Returns `{"stored", "duplicates", "edited", "deleted", "unmatched"}` counts.
Re-delivered messages are stored once.

| Platform | Handled | Ignored for now |
| --- | --- | --- |
| `slack` | `url_verification`; `message` events: plain (DMs are titled after the sender), `message_changed`, `message_deleted`, `tombstone` | other subtypes (joins, legacy bots, ...), other event types |
| `telegram` | `message` and `edited_message` updates with `text` | media and caption edits, other update types |

### Conversations

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/conversations` | Newest activity first. Query: `page`, `page_size`, `channel_id`, `unread=1`, `person_id` (conversations the person wrote in) |
| `GET` | `/api/conversations/<id>` | Includes `unread_count`, `last_message_preview` |
| `GET` | `/api/conversations/<id>/messages` | Newest first. Query: `page`, `page_size`, `include_deleted=1` |
| `POST` | `/api/conversations/<id>/messages` | `{"body"}`, max 4000 chars. Stores the reply, queues it and wakes the worker |
| `POST` | `/api/conversations/<id>/read` | Optional `{"up_to_message_id"}`; otherwise marks all inbound messages read |

Every message has `edited_at` and `deleted_at` (`null` unless the platform
reported an edit or deletion). Outbound messages carry `delivery_status`
(`pending`, `sent`, `failed`) and `delivery_error`; inbound ones have both
set to `null`.

### Messages

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/messages?q=` | Full-text search across all conversations, every word must match, newest first (see [Search](#search)). Each hit includes `conversation: {id, title, platform}`. Query: `match` (`word`, `prefix`), `person_id`, `platform`, `since`, `until`, `page`, `page_size`. The response echoes `query` and `match` |

### People and contacts

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/people` | Sorted by name. Each person includes `unread_count` and `contacts`. Query: `page`, `page_size` |
| `POST` | `/api/people` | `{"name", "contact_ids"?}`. All-or-nothing: an unknown (404) or already grouped (409) contact creates nothing |
| `GET` | `/api/people/<id>` | |
| `PATCH` | `/api/people/<id>` | `{"name"}` |
| `DELETE` | `/api/people/<id>` | Ungroups its contacts; messages are kept |
| `POST` | `/api/people/<id>/contacts` | `{"contact_ids": [...]}`; returns the person. Re-linking is a no-op, a contact on another person is a `409 contact_linked` |
| `DELETE` | `/api/people/<id>/contacts/<contact_id>` | Returns the person; 404 if the contact is not on it |
| `GET` | `/api/people/<id>/timeline` | The person's messages on every platform, newest first, with `conversation: {id, title, platform}`. Query: `page`, `page_size`, `include_deleted=1` |
| `GET` | `/api/contacts` | Accounts seen so far, sorted by name, each with `platform` and `person_id` (`null` = ungrouped). Query: `q` (name substring), `channel_id`, `person_id`, `unlinked=1`, `page`, `page_size` |
| `GET` | `/api/contacts/<id>` | |

### Outbox

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/outbox` | Queue entries newest first, with `attempts`, `next_attempt_at`, `last_error`. Query: `status`, `page`, `page_size` |
| `POST` | `/api/outbox/deliver` | Run one delivery pass now; returns `{"sent", "retried", "failed"}` |
| `POST` | `/api/outbox/<message_id>/retry` | Re-queue a failed message |

## Known gaps

- Grouping contacts into people is manual; the app does not propose matches
  (see below).
- Slack channel titles are the raw channel id (events don't carry names); DMs use the sender's name.
- Replies are plain text only; no attachments or threads, and replies cannot be edited or deleted from here.
- Telegram gives bots no deletion events, so messages deleted in Telegram stay in the inbox.
- Search has no stemming (`note` does not find `notes`; use `match=prefix`),
  no phrase queries, and no relevance ranking; FTS5 supports all three if
  wanted. Substring search was dropped with the move to FTS5.
- List endpoints use page/offset pagination.
- Discord is planned; it receives events over a gateway websocket rather than
  webhooks, so it will need a small long-running client in addition to a connector.

## Suggesting matches

Should the app propose that two contacts may be the same person, instead of
leaving the grouping entirely to the user? **Recommendation: not yet, and
when it comes, as dismissable hints from exact identifiers only, never
automatic grouping.** The reasoning:

- **The signals we have are weak.** Webhook events only carry display
  names: Slack sends `display_name`/`real_name`, Telegram a first and last
  name and maybe a username. Names collide ("Sam", "Alex") and differ across
  platforms for the same human (the seed data's "Mia" on Slack is "Mia
  Park" on Telegram). Anything stronger, such as an e-mail from Slack's
  `users.info` or a phone number from a Telegram `contact` share, needs
  extra scopes and API calls that the inbox does not make today.
- **The two kinds of mistake are not symmetric.** A missed match costs the
  user one manual link. A wrong match merges two strangers into one
  person: their unread count, conversation list and timeline become
  misleading, and in a shared inbox it could expose one contact's context
  next to another's. A suggestion that is wrong 20% of the time would
  also teach users to ignore it.
- **Manual grouping is already cheap at this scale.** A personal inbox has
  tens to a few hundred contacts. `GET /api/contacts?q=mia` sorted by name
  puts likely duplicates next to each other, which is most of what a
  name-based suggester would do, with the user's judgement in the loop.
- **Names are personal data.** Matching on them is a reasonable thing to
  do, but doing it silently and persistently (storing "we think these are
  the same") is a decision the user should make, not the app.

If usage shows people repeatedly grouping the same kind of pair, the
cheapest useful version is a `GET /api/people/suggestions` endpoint that
returns pairs of *ungrouped* contacts on *different* platforms whose
normalized display names (case-folded, punctuation stripped) are exactly
equal, with a way to dismiss a pair so it is not shown again. It should
stay suggestions-only: the link still goes through `POST /api/people`.
Fuzzy name matching, message-content analysis or automatic grouping are
not worth the false positives for this app.
