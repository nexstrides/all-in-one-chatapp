# All-in-One Chat

One inbox for conversations from several chat platforms. Each connected
account (a Slack workspace, a Telegram bot, ...) pushes events to a webhook;
the backend normalizes them into a single model of conversations and
messages, and a client reads and replies through one JSON API. Replies are
queued and delivered back to the platform by a small worker.

This repo is the backend only: Flask, SQLite and the standard library.
Requires Python 3.10+. Flask is the only third-party dependency.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python run.py                 # http://localhost:5000, creates instance/aiochat.sqlite3
python scripts/seed.py        # optional demo data (Slack + Telegram)
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

## Layout

```
aiochat/
  __init__.py        app factory
  config.py          settings from environment variables
  db.py, schema.sql  SQLite connection, schema and column migrations
  auth.py            bearer-token check for the client API
  errors.py          ApiError and JSON error responses
  worker.py          outbox delivery thread, `deliver-outbox` and `purge-deleted` CLI
  connectors/        one module per platform: webhook payload -> InboundMessage,
                     and send() for replies; http.py is the stdlib HTTP client
  services/          channels.py, inbox.py, outbox.py (all SQL lives here)
  api/               Flask blueprints: channels, conversations, messages, outbox, webhooks
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
| `GET` | `/api/conversations` | Newest activity first. Query: `page`, `page_size`, `channel_id`, `unread=1` |
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
| `GET` | `/api/messages?q=` | Case-insensitive substring search across all conversations, newest first. Each hit includes `conversation: {id, title, platform}`. Query: `page`, `page_size` |

### Outbox

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/outbox` | Queue entries newest first, with `attempts`, `next_attempt_at`, `last_error`. Query: `status`, `page`, `page_size` |
| `POST` | `/api/outbox/deliver` | Run one delivery pass now; returns `{"sent", "retried", "failed"}` |
| `POST` | `/api/outbox/<message_id>/retry` | Re-queue a failed message |

## Known gaps

- Contacts are per channel; the same person on Slack and Telegram is two contacts.
- Slack channel titles are the raw channel id (events don't carry names); DMs use the sender's name.
- Replies are plain text only; no attachments or threads, and replies cannot be edited or deleted from here.
- Telegram gives bots no deletion events, so messages deleted in Telegram stay in the inbox.
- Search is a `LIKE` scan, fine for a personal inbox; switch to FTS5 if it grows.
- List endpoints use page/offset pagination.
- Discord is planned; it receives events over a gateway websocket rather than
  webhooks, so it will need a small long-running client in addition to a connector.
