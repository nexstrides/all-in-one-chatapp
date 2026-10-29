"""People: user-made groups of contacts that are the same human.

A contact is one account on one channel. Grouping is entirely manual: the
user creates a person and links contacts to it; nothing here guesses. A
contact belongs to at most one person, and deleting a person only ungroups
its contacts (``ON DELETE SET NULL``).

Reading a person's messages across platforms lives in ``inbox.py``
(``list_person_messages``, and the ``person_id`` filters), which imports
``get_person_row`` from here.
"""

from __future__ import annotations

import sqlite3

from ..db import get_db
from ..errors import ApiError
from ..textmatch import like_pattern

# Unread inbound messages written by any of the person's contacts.
_PERSON_SELECT = """
SELECT
    p.id,
    p.name,
    p.created_at,
    (SELECT COUNT(*) FROM messages m
      JOIN contacts ct ON ct.id = m.contact_id
      WHERE ct.person_id = p.id AND m.direction = 'inbound'
        AND m.read_at IS NULL AND m.deleted_at IS NULL)
        AS unread_count
FROM people p
"""

_CONTACT_SELECT = """
SELECT ct.id, ct.channel_id, ch.platform, ct.external_user_id, ct.display_name,
       ct.person_id, ct.created_at
FROM contacts ct
JOIN channels ch ON ch.id = ct.channel_id
"""


def _contact_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "platform": row["platform"],
        "external_user_id": row["external_user_id"],
        "display_name": row["display_name"],
        "person_id": row["person_id"],
        "created_at": row["created_at"],
    }


def _person_to_dict(row: sqlite3.Row, contacts: list[dict]) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "unread_count": row["unread_count"],
        "contacts": contacts,
        "created_at": row["created_at"],
    }


def _contacts_by_person(db: sqlite3.Connection, person_ids: list[int]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {pid: [] for pid in person_ids}
    if not person_ids:
        return grouped
    placeholders = ", ".join("?" for _ in person_ids)
    rows = db.execute(
        f"{_CONTACT_SELECT} WHERE ct.person_id IN ({placeholders})"
        " ORDER BY ch.platform, ct.display_name COLLATE NOCASE, ct.id",
        person_ids,
    ).fetchall()
    for row in rows:
        grouped[row["person_id"]].append(_contact_to_dict(row))
    return grouped


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------

def get_person_row(person_id: int) -> sqlite3.Row:
    row = get_db().execute(f"{_PERSON_SELECT} WHERE p.id = ?", (person_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "Person not found.")
    return row


def get_person(person_id: int) -> dict:
    row = get_person_row(person_id)
    return _person_to_dict(row, _contacts_by_person(get_db(), [person_id])[person_id])


def list_people(page: int, page_size: int) -> dict:
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    rows = db.execute(
        f"{_PERSON_SELECT} ORDER BY p.name COLLATE NOCASE, p.id LIMIT ? OFFSET ?",
        (page_size, (page - 1) * page_size),
    ).fetchall()
    contacts = _contacts_by_person(db, [r["id"] for r in rows])
    return {
        "items": [_person_to_dict(r, contacts[r["id"]]) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def create_person(name: str, contact_ids: list[int] | None = None) -> dict:
    """Create a person, optionally linking contacts in the same transaction.

    If any contact cannot be linked, nothing is created.
    """
    db = get_db()
    try:
        cur = db.execute("INSERT INTO people (name) VALUES (?)", (name,))
        person_id = cur.lastrowid
        if contact_ids:
            _link(db, person_id, contact_ids)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_person(person_id)


def update_person(person_id: int, name: str) -> dict:
    get_person_row(person_id)
    db = get_db()
    db.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))
    db.commit()
    return get_person(person_id)


def delete_person(person_id: int) -> None:
    """Remove the person; its contacts become ungrouped again."""
    get_person_row(person_id)
    db = get_db()
    db.execute("DELETE FROM people WHERE id = ?", (person_id,))
    db.commit()


# --------------------------------------------------------------------------
# Linking contacts
# --------------------------------------------------------------------------

def _link(db: sqlite3.Connection, person_id: int, contact_ids: list[int]) -> None:
    """Attach contacts to a person. Caller owns the transaction.

    Linking a contact that already belongs to this person is a no-op; one
    that belongs to another person is refused rather than silently moved
    (unlink it first), so a stale client cannot regroup by accident.
    """
    for contact_id in contact_ids:
        row = db.execute("SELECT person_id FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found", f"Contact {contact_id} not found.")
        if row["person_id"] not in (None, person_id):
            raise ApiError(
                409,
                "contact_linked",
                f"Contact {contact_id} already belongs to person {row['person_id']}; unlink it first.",
            )
    placeholders = ", ".join("?" for _ in contact_ids)
    db.execute(
        f"UPDATE contacts SET person_id = ? WHERE id IN ({placeholders})", [person_id, *contact_ids]
    )


def link_contacts(person_id: int, contact_ids: list[int]) -> dict:
    get_person_row(person_id)
    db = get_db()
    try:
        _link(db, person_id, contact_ids)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_person(person_id)


def unlink_contact(person_id: int, contact_id: int) -> dict:
    get_person_row(person_id)
    db = get_db()
    cur = db.execute(
        "UPDATE contacts SET person_id = NULL WHERE id = ? AND person_id = ?",
        (contact_id, person_id),
    )
    db.commit()
    if cur.rowcount == 0:
        raise ApiError(404, "not_found", "Contact is not linked to this person.")
    return get_person(person_id)


# --------------------------------------------------------------------------
# Contacts
# --------------------------------------------------------------------------

def get_contact(contact_id: int) -> dict:
    row = get_db().execute(f"{_CONTACT_SELECT} WHERE ct.id = ?", (contact_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "Contact not found.")
    return _contact_to_dict(row)


def list_contacts(
    page: int,
    page_size: int,
    channel_id: int | None = None,
    person_id: int | None = None,
    unlinked: bool = False,
    query: str | None = None,
) -> dict:
    """Contacts by display name. ``query`` is a case-insensitive substring
    of the name; ``unlinked`` keeps only contacts not yet grouped."""
    where, params = [], []
    if channel_id is not None:
        where.append("ct.channel_id = ?")
        params.append(channel_id)
    if person_id is not None:
        where.append("ct.person_id = ?")
        params.append(person_id)
    if unlinked:
        where.append("ct.person_id IS NULL")
    if query:
        where.append("ct.display_name LIKE ? ESCAPE '\\'")
        params.append(like_pattern(query))
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

    db = get_db()
    total = db.execute(
        f"SELECT COUNT(*) FROM contacts ct JOIN channels ch ON ch.id = ct.channel_id{where_sql}",
        params,
    ).fetchone()[0]
    rows = db.execute(
        f"{_CONTACT_SELECT}{where_sql}"
        " ORDER BY ct.display_name COLLATE NOCASE, ch.platform, ct.id"
        " LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return {
        "items": [_contact_to_dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
    }
