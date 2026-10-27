"""SQLite connection handling.

One connection per request, stored on ``flask.g`` and closed at teardown.
"""

import os
import sqlite3
from importlib import resources

import click
from flask import current_app, g


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


def init_db() -> None:
    path = current_app.config["DATABASE"]
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    schema = resources.files("aiochat").joinpath("schema.sql").read_text()
    get_db().executescript(schema)


@click.command("init-db")
def init_db_command() -> None:
    """Create tables if they do not exist."""
    init_db()
    click.echo("Initialized the database.")


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
