"""SQLite connection handling.

One connection per request, stored on ``flask.g`` and closed at teardown.
"""

import os
import sqlite3
from importlib import resources

import click
from flask import current_app, g
from flask.cli import with_appcontext


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# Columns added after the first release. schema.sql includes them for new
# databases; databases created earlier get them here via ALTER TABLE. This
# must run before schema.sql because the script also creates indexes on them.
_ADDED_COLUMNS = (
    ("channels", "credential", "TEXT"),
    ("outbox", "next_attempt_at", "TEXT"),
    ("messages", "edited_at", "TEXT"),
    ("messages", "deleted_at", "TEXT"),
    # SQLite lets a REFERENCES clause point at a table that does not exist
    # yet (people is created by schema.sql right after); it only checks the
    # constraint on writes, and a NULL default never violates it.
    ("contacts", "person_id", "INTEGER REFERENCES people (id) ON DELETE SET NULL"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, declaration in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:  # empty set = table not created yet
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    conn.commit()


def _require_fts5(conn: sqlite3.Connection) -> None:
    """Search needs SQLite's FTS5 extension, which standard builds include."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.fts5_probe")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"This SQLite ({sqlite3.sqlite_version}) was built without FTS5, which message"
            " search requires. Use a Python whose sqlite3 module has it (python.org,"
            " Debian/Ubuntu, Homebrew and conda builds all do)."
        ) from exc


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def rebuild_search_index(conn: sqlite3.Connection) -> int:
    """Re-index every message body from scratch. Returns how many rows."""
    conn.execute("INSERT INTO messages_fts (messages_fts) VALUES ('rebuild')")
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def init_db() -> None:
    path = current_app.config["DATABASE"]
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    schema = resources.files("aiochat").joinpath("schema.sql").read_text()
    conn = get_db()
    _require_fts5(conn)
    _add_missing_columns(conn)
    had_index = _table_exists(conn, "messages_fts")
    # One transaction instead of an fsync per statement.
    conn.executescript(f"BEGIN;\n{schema}\nCOMMIT;")
    if not had_index and _table_exists(conn, "messages_fts"):
        # A database from before full-text search: the triggers only cover
        # rows written from now on, so index what is already there.
        rebuild_search_index(conn)


@click.command("init-db")
@with_appcontext
def init_db_command() -> None:
    """Create tables if they do not exist."""
    init_db()
    click.echo("Initialized the database.")


@click.command("reindex-search")
@with_appcontext
def reindex_search_command() -> None:
    """Rebuild the full-text index from the stored messages."""
    count = rebuild_search_index(get_db())
    click.echo(f"indexed={count}")


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(reindex_search_command)
