# All-in-One Chat

One inbox for conversations from several chat platforms. Each connected
account (a Slack workspace, a Telegram bot, ...) pushes events to a webhook;
the backend normalizes them into a single model of conversations and
messages, and a client reads and replies through one JSON API.

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

## Layout

```
aiochat/
  __init__.py        app factory
  config.py          settings from environment variables
  db.py, schema.sql  SQLite connection and schema
  auth.py            bearer-token check for the client API
  errors.py          ApiError and JSON error responses
  connectors/        one module per platform: webhook payload -> InboundMessage
  services/          channels.py, inbox.py (all SQL lives here)
  api/               Flask blueprints: channels, conversations, webhooks
scripts/seed.py      replays sample webhooks to load demo data
tests/               unittest suite
```

## API

All `/api/*` routes except webhooks need `Authorization: Bearer <AIOCHAT_API_TOKEN>`
when a token is configured. Errors look like
`{"error": {"code": "not_found", "message": "..."}}`.

### Channels

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/channels` | List connected accounts |
| `POST` | `/api/channels` | `{"platform", "name", "external_account_id"}`; the response includes `webhook_token`, shown only once |
| `GET` | `/api/channels/<id>` | |
| `DELETE` | `/api/channels/<id>` | Also deletes its conversations and messages |

### Webhooks

`POST /api/webhooks/<platform>/<channel_id>` with header
`X-Webhook-Token: <webhook_token>`. Returns `{"stored": n, "duplicates": m}`.
Re-delivered messages are stored once.

| Platform | Handled | Ignored for now |
| --- | --- | --- |
| `slack` | `url_verification`, `message` events | events with a `subtype` (edits, deletes, bots), other event types |
| `telegram` | `message` updates with `text` | media, `edited_message`, other update types |

### Conversations

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/conversations` | Newest activity first. Query: `page`, `page_size`, `channel_id`, `unread=1` |
| `GET` | `/api/conversations/<id>` | Includes `unread_count`, `last_message_preview` |
| `GET` | `/api/conversations/<id>/messages` | Newest first. Query: `page`, `page_size` |
| `POST` | `/api/conversations/<id>/messages` | `{"body"}`, max 4000 chars. Stores the reply and queues it in `outbox` |
| `POST` | `/api/conversations/<id>/read` | Optional `{"up_to_message_id"}`; otherwise marks all inbound messages read |

## Known gaps

- Replies are queued in `outbox` but nothing delivers them to the platforms yet.
- Contacts are per channel; the same person on Slack and Telegram is two contacts.
- No search across messages.
- List endpoints use page/offset pagination.
