"""Background delivery of the outbox.

Two ways to run delivery passes:

* ``start_outbox_worker(app)`` starts a daemon thread inside the server
  process (what ``run.py`` does). Replies nudge it so they go out at once;
  otherwise it polls every ``OUTBOX_INTERVAL`` seconds for retries.
* ``flask --app run deliver-outbox [--loop]`` runs passes from the command
  line, for cron or a dedicated process next to a multi-process server.

Both are safe to combine: ``deliver_pending`` claims rows atomically.
"""

from __future__ import annotations

import threading
import time

import click
from flask import current_app
from flask.cli import with_appcontext

from .services.inbox import purge_deleted
from .services.outbox import deliver_pending


class OutboxWorker(threading.Thread):
    def __init__(self, app, interval: float):
        super().__init__(name="outbox-worker", daemon=True)
        self.app = app
        self.interval = interval
        self._wake = threading.Event()
        self._stopping = threading.Event()  # not "_stop": Thread uses that name

    def run(self) -> None:
        while not self._stopping.is_set():
            self.run_once()
            self._wake.wait(self.interval)
            self._wake.clear()

    def run_once(self) -> dict:
        try:
            with self.app.app_context():
                purge_deleted()  # cheap housekeeping; see DELETED_RETENTION_DAYS
                return deliver_pending()
        except Exception:  # keep the thread alive; the pass will be retried
            self.app.logger.exception("outbox delivery pass failed")
            return {}

    def wake(self) -> None:
        """Run a pass as soon as possible (called after a reply is queued)."""
        self._wake.set()

    def stop(self, timeout: float | None = 5) -> None:
        self._stopping.set()
        self._wake.set()
        self.join(timeout)


def start_outbox_worker(app) -> OutboxWorker:
    worker = OutboxWorker(app, app.config["OUTBOX_INTERVAL"])
    worker.start()
    app.extensions["outbox_worker"] = worker
    return worker


def wake_outbox_worker(app) -> None:
    worker = app.extensions.get("outbox_worker")
    if worker is not None:
        worker.wake()


@click.command("deliver-outbox")
@with_appcontext
@click.option("--loop", is_flag=True, help="Keep running, polling every OUTBOX_INTERVAL seconds.")
def deliver_outbox_command(loop: bool) -> None:
    """Send queued replies to their platforms."""
    interval = current_app.config["OUTBOX_INTERVAL"]
    while True:
        result = deliver_pending()
        click.echo(
            f"sent={result['sent']} retried={result['retried']} failed={result['failed']}"
        )
        if not loop:
            break
        time.sleep(interval)


@click.command("purge-deleted")
@with_appcontext
def purge_deleted_command() -> None:
    """Remove messages deleted on the platform longer ago than the retention."""
    removed = purge_deleted()
    click.echo(f"removed={removed} retention_days={current_app.config['DELETED_RETENTION_DAYS']}")
