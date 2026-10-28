"""Application configuration.

Values come from environment variables so nothing sensitive lives in the repo.
Tests override these through ``create_app(test_config=...)``.
"""

import os


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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

    # Outbound delivery. run.py starts an in-process worker thread when
    # OUTBOX_WORKER is on; with several server processes, turn it off and run
    # `flask --app run deliver-outbox --loop` once instead.
    OUTBOX_WORKER = _env_bool("AIOCHAT_OUTBOX_WORKER", True)
    OUTBOX_INTERVAL = float(os.environ.get("AIOCHAT_OUTBOX_INTERVAL", "15"))  # seconds
    OUTBOX_MAX_ATTEMPTS = int(os.environ.get("AIOCHAT_OUTBOX_MAX_ATTEMPTS", "5"))

    # Messages deleted on the platform are hidden, not removed, and purged
    # for good after this many days. 0 keeps them forever.
    DELETED_RETENTION_DAYS = int(os.environ.get("AIOCHAT_DELETED_RETENTION_DAYS", "30"))

    # Timeout for calls to platform APIs, in seconds.
    HTTP_TIMEOUT = float(os.environ.get("AIOCHAT_HTTP_TIMEOUT", "10"))
