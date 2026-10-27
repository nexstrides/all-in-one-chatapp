"""Application configuration.

Values come from environment variables so nothing sensitive lives in the repo.
Tests override these through ``create_app(test_config=...)``.
"""

import os


class Config:
    # Path to the SQLite database file. ":memory:" is not supported because
    # each request opens its own connection; tests use a temp file instead.
    DATABASE = os.environ.get("AIOCHAT_DATABASE", "instance/aiochat.sqlite3")

    # Bearer token required on every /api route except inbound webhooks.
    # Leave unset in development to disable API auth entirely.
    API_TOKEN = os.environ.get("AIOCHAT_API_TOKEN")

    # Default and maximum page sizes for list endpoints.
    DEFAULT_PAGE_SIZE = int(os.environ.get("AIOCHAT_DEFAULT_PAGE_SIZE", "25"))
    MAX_PAGE_SIZE = int(os.environ.get("AIOCHAT_MAX_PAGE_SIZE", "100"))
