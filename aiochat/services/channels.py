"""Connected channels (one per external account)."""

from __future__ import annotations

import secrets
import sqlite3

from ..connectors import CONNECTORS
from ..db import get_db
from ..errors import ApiError

_UNSET = object()  # "argument not given" for update_channel


def _to_dict(row: sqlite3.Row, include_token: bool = False) -> dict:
    data = {
        "id": row["id"],
        "platform": row["platform"],
        "name": row["name"],
        "external_account_id": row["external_account_id"],
        # The credential itself is never returned, only whether one is set.
        "can_send": row["credential"] is not None,
        "created_at": row["created_at"],
    }
    if include_token:
        data["webhook_token"] = row["webhook_token"]
    return data


def create_channel(
    platform: str, name: str, external_account_id: str, credential: str | None = None
) -> dict:
    if platform not in CONNECTORS:
        supported = ", ".join(sorted(CONNECTORS))
        raise ApiError(400, "unsupported_platform", f"Platform must be one of: {supported}.")

    db = get_db()
    token = secrets.token_urlsafe(24)
    try:
        cur = db.execute(
            "INSERT INTO channels (platform, name, external_account_id, webhook_token, credential)"
            " VALUES (?, ?, ?, ?, ?)",
            (platform, name, external_account_id, token, credential),
        )
        db.commit()
    except sqlite3.IntegrityError:
        raise ApiError(
            409, "channel_exists", "This account is already connected."
        ) from None

    row = db.execute("SELECT * FROM channels WHERE id = ?", (cur.lastrowid,)).fetchone()
    # The token is only ever shown once, at creation time.
    return _to_dict(row, include_token=True)


def list_channels() -> list[dict]:
    rows = get_db().execute("SELECT * FROM channels ORDER BY id").fetchall()
    return [_to_dict(r) for r in rows]


def get_channel_row(channel_id: int) -> sqlite3.Row:
    row = get_db().execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "Channel not found.")
    return row


def get_channel(channel_id: int) -> dict:
    return _to_dict(get_channel_row(channel_id))


def update_channel(channel_id: int, name=_UNSET, credential=_UNSET) -> dict:
    """Change the display name and/or credential. ``credential=None`` clears it."""
    get_channel_row(channel_id)
    sets, params = [], []
    if name is not _UNSET:
        sets.append("name = ?")
        params.append(name)
    if credential is not _UNSET:
        sets.append("credential = ?")
        params.append(credential)
    if sets:
        db = get_db()
        db.execute(f"UPDATE channels SET {', '.join(sets)} WHERE id = ?", [*params, channel_id])
        db.commit()
    return get_channel(channel_id)


def delete_channel(channel_id: int) -> None:
    get_channel_row(channel_id)
    db = get_db()
    db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    db.commit()
