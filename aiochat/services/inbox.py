"""The unified inbox: storing inbound messages and reading conversations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from flask import current_app

from ..connectors import CONNECTORS, InboundDelete, InboundEdit, InboundEvent, InboundMessage
from ..connectors.base import iso_utc
from ..db import get_db
from ..errors import ApiError
from ..textmatch import DEFAULT_MODE, MODES, fts_query
from .people import get_person_row


def utc_now_iso() -> str:
    return iso_utc(datetime.now(timezone.utc))


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


def _recompute_last_message_at(db: sqlite3.Connection, conversation_id: int) -> None:
    """After a deletion the newest visible message may have changed."""
    db.execute(
        "UPDATE conversations SET last_message_at ="
        " (SELECT MAX(sent_at) FROM messages WHERE conversation_id = ? AND deleted_at IS NULL)"
        " WHERE id = ?",
        (conversation_id, conversation_id),
    )


def _find_message(db: sqlite3.Connection, channel_id: int, event) -> sqlite3.Row | None:
    return db.execute(
        "SELECT m.id, m.conversation_id, m.body, m.deleted_at FROM messages m"
        " JOIN conversations c ON c.id = m.conversation_id"
        " WHERE c.channel_id = ? AND c.external_conversation_id = ? AND m.external_message_id = ?",
        (channel_id, event.external_conversation_id, event.external_message_id),
    ).fetchone()


def _ingest_message(db: sqlite3.Connection, channel_id: int, msg: InboundMessage) -> str:
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
        return "duplicates"  # re-delivery; also covers soft-deleted originals
    _touch_conversation(db, conversation_id, msg.sent_at)
    return "stored"


def _ingest_edit(db: sqlite3.Connection, channel_id: int, edit: InboundEdit) -> str:
    row = _find_message(db, channel_id, edit)
    if row is None:
        return "unmatched"  # edit of something we never stored (or already purged)
    if row["deleted_at"] is not None or row["body"] == edit.body:
        # Slack sends message_changed for link unfurls and other
        # attachment changes where the text is the same; not an edit.
        return "duplicates"
    # read_at and sent_at are untouched: an edit neither re-flags the message
    # as unread nor moves the conversation up the list.
    db.execute(
        "UPDATE messages SET body = ?, edited_at = ? WHERE id = ?",
        (edit.body, edit.edited_at, row["id"]),
    )
    return "edited"


def _ingest_delete(db: sqlite3.Connection, channel_id: int, delete: InboundDelete) -> str:
    row = _find_message(db, channel_id, delete)
    if row is None:
        return "unmatched"
    if row["deleted_at"] is not None:
        return "duplicates"
    db.execute("UPDATE messages SET deleted_at = ? WHERE id = ?", (delete.deleted_at, row["id"]))
    _recompute_last_message_at(db, row["conversation_id"])
    return "deleted"


def ingest(channel_id: int, events: list[InboundEvent]) -> dict:
    """Apply inbound events for a channel and return counts by outcome.

    ``stored``/``edited``/``deleted`` are applied changes; ``duplicates`` are
    re-deliveries or no-ops; ``unmatched`` are edits or deletions of
    messages we do not have.
    """
    db = get_db()
    counts = {"stored": 0, "duplicates": 0, "edited": 0, "deleted": 0, "unmatched": 0}
    try:
        for event in events:
            if isinstance(event, InboundMessage):
                outcome = _ingest_message(db, channel_id, event)
            elif isinstance(event, InboundEdit):
                outcome = _ingest_edit(db, channel_id, event)
            elif isinstance(event, InboundDelete):
                outcome = _ingest_delete(db, channel_id, event)
            else:  # pragma: no cover - connectors only produce the three above
                raise TypeError(f"unknown inbound event {type(event).__name__}")
            counts[outcome] += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return counts


def purge_deleted(retention_days: int | None = None) -> int:
    """Permanently remove messages soft-deleted longer ago than the retention.

    Returns how many rows were removed. A retention of 0 keeps them forever.
    """
    if retention_days is None:
        retention_days = current_app.config["DELETED_RETENTION_DAYS"]
    if retention_days <= 0:
        return 0
    cutoff_iso = iso_utc(datetime.now(timezone.utc) - timedelta(days=retention_days))
    db = get_db()
    cur = db.execute(
        "DELETE FROM messages WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff_iso,)
    )
    db.commit()
    return cur.rowcount


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
      WHERE m.conversation_id = c.id AND m.direction = 'inbound'
        AND m.read_at IS NULL AND m.deleted_at IS NULL)
        AS unread_count,
    (SELECT m.body FROM messages m
      WHERE m.conversation_id = c.id AND m.deleted_at IS NULL
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
    person_id: int | None = None,
) -> dict:
    """``person_id`` keeps conversations in which one of that person's
    contacts has written at least one (non-deleted) message."""
    where, params = [], []
    if channel_id is not None:
        where.append("c.channel_id = ?")
        params.append(channel_id)
    if person_id is not None:
        get_person_row(person_id)
        where.append(
            "EXISTS (SELECT 1 FROM messages m JOIN contacts ct ON ct.id = m.contact_id"
            " WHERE m.conversation_id = c.id AND ct.person_id = ? AND m.deleted_at IS NULL)"
        )
        params.append(person_id)
    if unread_only:
        where.append(
            "EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id"
            " AND m.direction = 'inbound' AND m.read_at IS NULL AND m.deleted_at IS NULL)"
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
        "edited_at": row["edited_at"],
        "deleted_at": row["deleted_at"],
        "delivery_status": row["delivery_status"],
        "delivery_error": row["delivery_error"],
    }


_MESSAGE_SELECT = """
SELECT m.*, ct.display_name, o.status AS delivery_status, o.last_error AS delivery_error
FROM messages m
LEFT JOIN contacts ct ON ct.id = m.contact_id
LEFT JOIN outbox o ON o.message_id = m.id
"""


def list_messages(
    conversation_id: int, page: int, page_size: int, include_deleted: bool = False
) -> dict:
    """Messages newest-first, so page 1 is the most recent screenful.

    Deleted messages are hidden unless ``include_deleted`` is set, in which
    case they appear in place with ``deleted_at`` set.
    """
    get_conversation(conversation_id)
    visible = "" if include_deleted else " AND m.deleted_at IS NULL"
    db = get_db()
    total = db.execute(
        f"SELECT COUNT(*) FROM messages m WHERE m.conversation_id = ?{visible}", (conversation_id,)
    ).fetchone()[0]
    rows = db.execute(
        f"{_MESSAGE_SELECT} WHERE m.conversation_id = ?{visible}"
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


# Messages with their conversation's title and platform, for listings that
# span conversations (search, a person's timeline).
_MESSAGE_WITH_CONVERSATION_SELECT = """
SELECT m.*, ct.display_name, o.status AS delivery_status, o.last_error AS delivery_error,
       c.title AS conversation_title, ch.platform
FROM messages m
JOIN conversations c ON c.id = m.conversation_id
JOIN channels ch ON ch.id = c.channel_id
LEFT JOIN contacts ct ON ct.id = m.contact_id
LEFT JOIN outbox o ON o.message_id = m.id
"""

_MESSAGE_WITH_CONVERSATION_COUNT = """
SELECT COUNT(*)
FROM messages m
JOIN conversations c ON c.id = m.conversation_id
JOIN channels ch ON ch.id = c.channel_id
"""


def _message_with_conversation_to_dict(row: sqlite3.Row) -> dict:
    item = _message_to_dict(row)
    item["conversation"] = {
        "id": row["conversation_id"],
        "title": row["conversation_title"],
        "platform": row["platform"],
    }
    return item


def _messages_across_conversations(where: list[str], params: list, page: int, page_size: int) -> dict:
    db = get_db()
    where_sql = " WHERE " + " AND ".join(where)
    total = db.execute(f"{_MESSAGE_WITH_CONVERSATION_COUNT}{where_sql}", params).fetchone()[0]
    rows = db.execute(
        f"{_MESSAGE_WITH_CONVERSATION_SELECT}{where_sql}"
        " ORDER BY m.sent_at DESC, m.id DESC"
        " LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return {
        "items": [_message_with_conversation_to_dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def search_messages(
    query: str,
    page: int,
    page_size: int,
    match: str = DEFAULT_MODE,
    person_id: int | None = None,
    channel_id: int | None = None,
    platform: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """Full-text search over all messages, newest first.

    Every word of ``query`` must occur in the message (any order); ``match``
    is ``word`` (whole words, the default) or ``prefix``, see textmatch.py.
    ``person_id`` keeps messages written by that person's contacts;
    ``channel_id`` behaves like it does on conversations (an unknown id just
    matches nothing); ``since``/``until`` are inclusive bounds on ``sent_at``
    in the stored ISO format (the API layer normalizes user input).
    """
    if match not in MODES:
        raise ApiError(400, "invalid_parameter", f"'match' must be one of: {', '.join(MODES)}.")
    if platform is not None and platform not in CONNECTORS:
        supported = ", ".join(sorted(CONNECTORS))
        raise ApiError(400, "invalid_parameter", f"'platform' must be one of: {supported}.")

    expression = fts_query(query, match)
    if expression is None:  # nothing but punctuation: no word can match
        return {"items": [], "page": page, "page_size": page_size, "total": 0,
                "query": query, "match": match}

    where = [
        "m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)",
        "m.deleted_at IS NULL",
    ]
    params: list = [expression]
    if person_id is not None:
        get_person_row(person_id)
        where.append("m.contact_id IN (SELECT id FROM contacts WHERE person_id = ?)")
        params.append(person_id)
    if channel_id is not None:
        where.append("c.channel_id = ?")
        params.append(channel_id)
    if platform is not None:
        where.append("ch.platform = ?")
        params.append(platform)
    if since is not None:
        where.append("m.sent_at >= ?")
        params.append(since)
    if until is not None:
        where.append("m.sent_at <= ?")
        params.append(until)

    result = _messages_across_conversations(where, params, page, page_size)
    result["query"] = query
    result["match"] = match
    return result


def list_person_messages(
    person_id: int, page: int, page_size: int, include_deleted: bool = False
) -> dict:
    """One timeline of everything a person wrote, on any platform, newest first."""
    get_person_row(person_id)
    where = ["m.contact_id IN (SELECT id FROM contacts WHERE person_id = ?)"]
    if not include_deleted:
        where.append("m.deleted_at IS NULL")
    result = _messages_across_conversations(where, [person_id], page, page_size)
    result["person_id"] = person_id
    return result


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
        " AND deleted_at IS NULL"
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

    row = db.execute(f"{_MESSAGE_SELECT} WHERE m.id = ?", (message_id,)).fetchone()
    return _message_to_dict(row)
