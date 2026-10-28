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
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, declaration in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:  # empty set = table not created yet
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    conn.commit()


def init_db() -> None:
    path = current_app.config["DATABASE"]
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    schema = resources.files("aiochat").joinpath("schema.sql").read_text()
    conn = get_db()
    _add_missing_columns(conn)
    # One transaction instead of an fsync per statement.
    conn.executescript(f"BEGIN;\n{schema}\nCOMMIT;")


@click.command("init-db")
@with_appcontext
def init_db_command() -> None:
    """Create tables if they do not exist."""
    init_db()
    click.echo("Initialized the database.")


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
