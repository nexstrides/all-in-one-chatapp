-- All-in-One Chat schema (SQLite).
-- A "channel" is one connected account on an external platform (a Slack
-- workspace, a Telegram bot, ...). Everything else hangs off a channel.

CREATE TABLE IF NOT EXISTS channels (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    platform            TEXT    NOT NULL,
    name                TEXT    NOT NULL,
    external_account_id TEXT    NOT NULL,
    webhook_token       TEXT    NOT NULL,
    -- Platform API token used to send replies (Slack bot token, Telegram bot
    -- token). NULL means the channel is receive-only.
    credential          TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (platform, external_account_id)
);

CREATE TABLE IF NOT EXISTS contacts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id       INTEGER NOT NULL REFERENCES channels (id) ON DELETE CASCADE,
    external_user_id TEXT    NOT NULL,
    display_name     TEXT    NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (channel_id, external_user_id)
);

CREATE TABLE IF NOT EXISTS conversations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id               INTEGER NOT NULL REFERENCES channels (id) ON DELETE CASCADE,
    external_conversation_id TEXT    NOT NULL,
    title                    TEXT    NOT NULL,
    last_message_at          TEXT,
    created_at               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (channel_id, external_conversation_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     INTEGER NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    contact_id          INTEGER REFERENCES contacts (id) ON DELETE SET NULL,
    direction           TEXT    NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    external_message_id TEXT,
    body                TEXT    NOT NULL,
    sent_at             TEXT    NOT NULL,
    read_at             TEXT,
    -- Set when the platform reports an edit; body then holds the new text.
    edited_at           TEXT,
    -- Soft delete: the row is hidden from the inbox but kept for a retention
    -- period (see purge_deleted) so history survives and re-deliveries of
    -- the original still deduplicate.
    deleted_at          TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Platforms retry webhook deliveries; the same external message must only be
-- stored once per conversation.
CREATE UNIQUE INDEX IF NOT EXISTS messages_external_unique
    ON messages (conversation_id, external_message_id)
    WHERE external_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS messages_conversation_sent
    ON messages (conversation_id, sent_at);

CREATE INDEX IF NOT EXISTS messages_deleted
    ON messages (deleted_at)
    WHERE deleted_at IS NOT NULL;

-- Outbound replies are queued here and delivered by services/outbox.py.
-- A worker claims a row by bumping `attempts` and pushing `next_attempt_at`
-- into the future (a lease), so several workers can share one database.
CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL UNIQUE REFERENCES messages (id) ON DELETE CASCADE,
    status          TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS outbox_pending
    ON outbox (status, next_attempt_at);
