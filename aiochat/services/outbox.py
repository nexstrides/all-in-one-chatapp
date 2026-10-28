"""Delivering queued replies to the platforms.

``deliver_pending`` is one delivery pass. It is safe to call from several
threads or processes sharing a database: each row is claimed with a short
lease (``attempts`` bumped, ``next_attempt_at`` pushed into the future) in a
single conditional UPDATE before anything is sent.

Rows for channels without a credential are left alone; they become eligible
as soon as a credential is set.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from flask import current_app

from ..connectors import DeliveryError, get_connector
from ..connectors.base import iso_from_epoch
from ..db import get_db
from ..errors import ApiError
from .inbox import utc_now_iso

log = logging.getLogger(__name__)

# How long a claimed row is considered "in flight" before another pass may
# pick it up again (covers a crash between claim and result).
LEASE_SECONDS = 120

# Delay before retrying after the 1st, 2nd, 3rd, ... transient failure.
BACKOFF_SECONDS = (30, 120, 600, 1800)


def _backoff(attempt: int) -> float:
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]


def _claim(db: sqlite3.Connection, outbox_id: int, attempts: int, now: str) -> bool:
    cur = db.execute(
        "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ?, updated_at = ?"
        " WHERE id = ? AND status = 'pending' AND attempts = ?",
        (iso_from_epoch(time.time() + LEASE_SECONDS), now, outbox_id, attempts),
    )
    db.commit()
    return cur.rowcount == 1


def _record_sent(db: sqlite3.Connection, row: sqlite3.Row, external_id: str) -> None:
    try:
        db.execute(
            "UPDATE messages SET external_message_id = ? WHERE id = ?",
            (external_id, row["message_id"]),
        )
    except sqlite3.IntegrityError:
        # The platform echoed our own message back (Slack does this) and the
        # webhook stored it as inbound before we learned its id. Drop the echo.
        db.execute(
            "DELETE FROM messages WHERE conversation_id = ? AND external_message_id = ?"
            " AND direction = 'inbound'",
            (row["conversation_id"], external_id),
        )
        db.execute(
            "UPDATE messages SET external_message_id = ? WHERE id = ?",
            (external_id, row["message_id"]),
        )
    db.execute(
        "UPDATE outbox SET status = 'sent', next_attempt_at = NULL, last_error = NULL,"
        " updated_at = ? WHERE id = ?",
        (utc_now_iso(), row["outbox_id"]),
    )


def _record_failure(
    db: sqlite3.Connection, row: sqlite3.Row, attempt: int, error: str, retryable: bool
) -> str:
    """Schedule a retry or give up. Returns the resulting status."""
    max_attempts = current_app.config["OUTBOX_MAX_ATTEMPTS"]
    if retryable and attempt < max_attempts:
        db.execute(
            "UPDATE outbox SET next_attempt_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (iso_from_epoch(time.time() + _backoff(attempt)), error, utc_now_iso(), row["outbox_id"]),
        )
        return "pending"
    db.execute(
        "UPDATE outbox SET status = 'failed', next_attempt_at = NULL, last_error = ?,"
        " updated_at = ? WHERE id = ?",
        (error, utc_now_iso(), row["outbox_id"]),
    )
    return "failed"


def deliver_pending(limit: int = 50) -> dict:
    """Send due messages. Returns counts: sent, retried (scheduled again), failed."""
    db = get_db()
    now = utc_now_iso()
    rows = db.execute(
        "SELECT o.id AS outbox_id, o.attempts, m.id AS message_id, m.body,"
        "       c.id AS conversation_id, c.external_conversation_id, ch.platform, ch.credential"
        " FROM outbox o"
        " JOIN messages m ON m.id = o.message_id"
        " JOIN conversations c ON c.id = m.conversation_id"
        " JOIN channels ch ON ch.id = c.channel_id"
        " WHERE o.status = 'pending' AND ch.credential IS NOT NULL"
        "   AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= ?)"
        " ORDER BY o.id LIMIT ?",
        (now, limit),
    ).fetchall()

    counts = {"sent": 0, "retried": 0, "failed": 0}
    timeout = current_app.config["HTTP_TIMEOUT"]
    for row in rows:
        if not _claim(db, row["outbox_id"], row["attempts"], now):
            continue  # another worker got there first
        attempt = row["attempts"] + 1
        try:
            connector = get_connector(row["platform"])
            if connector is None:
                raise DeliveryError(f"no connector for platform '{row['platform']}'")
            external_id = connector.send(
                row["credential"], row["external_conversation_id"], row["body"], timeout=timeout
            )
        except DeliveryError as exc:
            status = _record_failure(db, row, attempt, str(exc), exc.retryable)
            counts["retried" if status == "pending" else "failed"] += 1
        except Exception as exc:  # a bug in a connector must not stop the queue
            log.exception("unexpected error sending message %s", row["message_id"])
            status = _record_failure(db, row, attempt, f"unexpected error: {exc!r}", True)
            counts["retried" if status == "pending" else "failed"] += 1
        else:
            _record_sent(db, row, external_id)
            counts["sent"] += 1
        db.commit()
    return counts


# --------------------------------------------------------------------------
# Inspection and manual control
# --------------------------------------------------------------------------

_STATUSES = ("pending", "sent", "failed")


def _entry_to_dict(row: sqlite3.Row) -> dict:
    return {
        "message_id": row["message_id"],
        "conversation_id": row["conversation_id"],
        "platform": row["platform"],
        "body": row["body"],
        "status": row["status"],
        "attempts": row["attempts"],
        "next_attempt_at": row["next_attempt_at"],
        "last_error": row["last_error"],
        "can_send": row["credential"] is not None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_entries(page: int, page_size: int, status: str | None = None) -> dict:
    if status is not None and status not in _STATUSES:
        raise ApiError(400, "invalid_parameter", f"'status' must be one of: {', '.join(_STATUSES)}.")
    where_sql, params = ("", [])
    if status is not None:
        where_sql, params = (" WHERE o.status = ?", [status])

    db = get_db()
    total = db.execute(f"SELECT COUNT(*) FROM outbox o{where_sql}", params).fetchone()[0]
    rows = db.execute(
        "SELECT o.*, m.body, c.id AS conversation_id, ch.platform, ch.credential"
        " FROM outbox o"
        " JOIN messages m ON m.id = o.message_id"
        " JOIN conversations c ON c.id = m.conversation_id"
        " JOIN channels ch ON ch.id = c.channel_id"
        f"{where_sql} ORDER BY o.id DESC LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return {
        "items": [_entry_to_dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def retry(message_id: int) -> dict:
    """Put a failed message back in the queue, due immediately."""
    db = get_db()
    cur = db.execute(
        "UPDATE outbox SET status = 'pending', attempts = 0, next_attempt_at = NULL,"
        " last_error = NULL, updated_at = ? WHERE message_id = ? AND status = 'failed'",
        (utc_now_iso(), message_id),
    )
    db.commit()
    if cur.rowcount == 0:
        if db.execute("SELECT 1 FROM outbox WHERE message_id = ?", (message_id,)).fetchone():
            raise ApiError(409, "not_failed", "Only failed messages can be retried.")
        raise ApiError(404, "not_found", "Outbox entry not found.")
    row = db.execute(
        "SELECT o.*, m.body, c.id AS conversation_id, ch.platform, ch.credential"
        " FROM outbox o"
        " JOIN messages m ON m.id = o.message_id"
        " JOIN conversations c ON c.id = m.conversation_id"
        " JOIN channels ch ON ch.id = c.channel_id"
        " WHERE o.message_id = ?",
        (message_id,),
    ).fetchone()
    return _entry_to_dict(row)
