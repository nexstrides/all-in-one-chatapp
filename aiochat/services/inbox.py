"""The unified inbox: storing inbound messages and reading conversations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..connectors import InboundMessage
from ..db import get_db
from ..errors import ApiError


def utc_now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------

def _upsert_contact(db: sqlite3.Connection, channel_id: int, msg: InboundMessage) -> int:
    db.execute(
        "INSERT INTO contacts (channel_id, external_user_id, display_name) VALUES (?, ?, ?)"
        " ON CONFLICT (channel_id, external_user_id) DO UPDATE SET display_name = excluded.display_name",
        (channel_id, msg.external_user_id, msg.user_display_name),
    )
    return db.execute(
        "SELECT id FROM contacts WHERE channel_id = ? AND external_user_id = ?",
        (channel_id, msg.external_user_id),
    ).fetchone()["id"]


def _upsert_conversation(db: sqlite3.Connection, channel_id: int, msg: InboundMessage) -> int:
    db.execute(
        "INSERT INTO conversations (channel_id, external_conversation_id, title) VALUES (?, ?, ?)"
        " ON CONFLICT (channel_id, external_conversation_id) DO NOTHING",
        (channel_id, msg.external_conversation_id, msg.conversation_title),
    )
    return db.execute(
        "SELECT id FROM conversations WHERE channel_id = ? AND external_conversation_id = ?",
        (channel_id, msg.external_conversation_id),
    ).fetchone()["id"]


def _touch_conversation(db: sqlite3.Connection, conversation_id: int, sent_at: str) -> None:
    # Messages can arrive out of order, so only move last_message_at forward.
    db.execute(
        "UPDATE conversations SET last_message_at = ?"
        " WHERE id = ? AND (last_message_at IS NULL OR last_message_at < ?)",
        (sent_at, conversation_id, sent_at),
    )


def ingest(channel_id: int, messages: list[InboundMessage]) -> dict:
    """Store inbound messages for a channel. Re-delivered messages are skipped."""
    db = get_db()
    stored = duplicates = 0
    try:
        for msg in messages:
            contact_id = _upsert_contact(db, channel_id, msg)
            conversation_id = _upsert_conversation(db, channel_id, msg)
            cur = db.execute(
                "INSERT INTO messages"
                " (conversation_id, contact_id, direction, external_message_id, body, sent_at)"
                " VALUES (?, ?, 'inbound', ?, ?, ?)"
                " ON CONFLICT DO NOTHING",
                (conversation_id, contact_id, msg.external_message_id, msg.body, msg.sent_at),
            )
            if cur.rowcount == 0:
                duplicates += 1
                continue
            stored += 1
            _touch_conversation(db, conversation_id, msg.sent_at)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"stored": stored, "duplicates": duplicates}


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

_CONVERSATION_SELECT = """
SELECT
    c.id,
    c.channel_id,
    ch.platform,
    c.title,
    c.last_message_at,
    c.created_at,
    (SELECT COUNT(*) FROM messages m
      WHERE m.conversation_id = c.id AND m.direction = 'inbound' AND m.read_at IS NULL)
        AS unread_count,
    (SELECT m.body FROM messages m
      WHERE m.conversation_id = c.id
      ORDER BY m.sent_at DESC, m.id DESC LIMIT 1) AS last_message_preview
FROM conversations c
JOIN channels ch ON ch.id = c.channel_id
"""


def _conversation_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "platform": row["platform"],
        "title": row["title"],
        "last_message_at": row["last_message_at"],
        "last_message_preview": row["last_message_preview"],
        "unread_count": row["unread_count"],
        "created_at": row["created_at"],
    }


def list_conversations(
    page: int,
    page_size: int,
    channel_id: int | None = None,
    unread_only: bool = False,
) -> dict:
    where, params = [], []
    if channel_id is not None:
        where.append("c.channel_id = ?")
        params.append(channel_id)
    if unread_only:
        where.append(
            "EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id"
            " AND m.direction = 'inbound' AND m.read_at IS NULL)"
        )
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

    db = get_db()
    total = db.execute(f"SELECT COUNT(*) FROM conversations c{where_sql}", params).fetchone()[0]
    rows = db.execute(
        f"{_CONVERSATION_SELECT}{where_sql}"
        " ORDER BY c.last_message_at IS NULL, c.last_message_at DESC, c.id DESC"
        " LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return {
        "items": [_conversation_to_dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def get_conversation(conversation_id: int) -> dict:
    row = get_db().execute(
        f"{_CONVERSATION_SELECT} WHERE c.id = ?", (conversation_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "Conversation not found.")
    return _conversation_to_dict(row)


def _message_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "direction": row["direction"],
        "sender": (
            {"contact_id": row["contact_id"], "display_name": row["display_name"]}
            if row["contact_id"] is not None
            else None
        ),
        "body": row["body"],
        "sent_at": row["sent_at"],
        "read_at": row["read_at"],
        "delivery_status": row["delivery_status"],
    }


def list_messages(conversation_id: int, page: int, page_size: int) -> dict:
    """Messages newest-first, so page 1 is the most recent screenful."""
    get_conversation(conversation_id)
    db = get_db()
    total = db.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()[0]
    rows = db.execute(
        "SELECT m.*, ct.display_name, o.status AS delivery_status"
        " FROM messages m"
        " LEFT JOIN contacts ct ON ct.id = m.contact_id"
        " LEFT JOIN outbox o ON o.message_id = m.id"
        " WHERE m.conversation_id = ?"
        " ORDER BY m.sent_at DESC, m.id DESC"
        " LIMIT ? OFFSET ?",
        (conversation_id, page_size, (page - 1) * page_size),
    ).fetchall()
    return {
        "items": [_message_to_dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def mark_read(conversation_id: int, up_to_message_id: int | None = None) -> int:
    """Mark inbound messages read. Returns how many changed."""
    get_conversation(conversation_id)
    db = get_db()
    sql = (
        "UPDATE messages SET read_at = ?"
        " WHERE conversation_id = ? AND direction = 'inbound' AND read_at IS NULL"
    )
    params: list = [utc_now_iso(), conversation_id]
    if up_to_message_id is not None:
        sql += " AND id <= ?"
        params.append(up_to_message_id)
    cur = db.execute(sql, params)
    db.commit()
    return cur.rowcount


def reply(conversation_id: int, body: str) -> dict:
    """Store an outbound message and queue it for delivery."""
    get_conversation(conversation_id)
    db = get_db()
    now = utc_now_iso()
    try:
        cur = db.execute(
            "INSERT INTO messages (conversation_id, direction, body, sent_at, read_at)"
            " VALUES (?, 'outbound', ?, ?, ?)",
            (conversation_id, body, now, now),
        )
        message_id = cur.lastrowid
        db.execute("INSERT INTO outbox (message_id) VALUES (?)", (message_id,))
        _touch_conversation(db, conversation_id, now)
        db.commit()
    except Exception:
        db.rollback()
        raise

    row = db.execute(
        "SELECT m.*, NULL AS display_name, o.status AS delivery_status"
        " FROM messages m JOIN outbox o ON o.message_id = m.id WHERE m.id = ?",
        (message_id,),
    ).fetchone()
    return _message_to_dict(row)
