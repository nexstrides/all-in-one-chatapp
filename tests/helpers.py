"""Shared test setup: a fresh app and database per test."""

import os
import tempfile
import time
import unittest

from aiochat import create_app

API_TOKEN = "test-token"

# Pin every setting the tests depend on, so AIOCHAT_* variables in the
# developer's shell cannot change the outcome.
TEST_CONFIG = {
    "TESTING": True,
    "API_TOKEN": API_TOKEN,
    "DEFAULT_PAGE_SIZE": 25,
    "MAX_PAGE_SIZE": 100,
    "OUTBOX_WORKER": False,
    "OUTBOX_INTERVAL": 60.0,
    "OUTBOX_MAX_ATTEMPTS": 5,
    "HTTP_TIMEOUT": 10.0,
}


class AppTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.app = create_app({**TEST_CONFIG, "DATABASE": self.db_path})
        self.client = self.app.test_client()

    def tearDown(self):
        worker = self.app.extensions.pop("outbox_worker", None)
        if worker is not None:
            worker.stop()
        # Windows refuses to delete a file another handle still has open, and
        # SQLite can release its handle a moment after close(); retry briefly.
        for _ in range(50):
            try:
                os.unlink(self.db_path)
                break
            except PermissionError:
                time.sleep(0.02)

    # -- HTTP helpers -----------------------------------------------------

    def api(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {})
        headers.setdefault("Authorization", f"Bearer {API_TOKEN}")
        return self.client.open(path, method=method, headers=headers, **kwargs)

    def create_channel(self, platform="slack", name="Acme", account="T1", credential=None):
        body = {"platform": platform, "name": name, "external_account_id": account}
        if credential is not None:
            body["credential"] = credential
        resp = self.api("POST", "/api/channels", json=body)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()

    def send_webhook(self, channel, payload, token=None):
        return self.client.post(
            f"/api/webhooks/{channel['platform']}/{channel['id']}",
            json=payload,
            headers={"X-Webhook-Token": token if token is not None else channel["webhook_token"]},
        )


def slack_message(channel="C1", user="U1", text="hello", ts="1761500000.000100", **event):
    return {
        "type": "event_callback",
        "team_id": "T1",
        "event": {"type": "message", "channel": channel, "user": user, "text": text, "ts": ts, **event},
    }


def telegram_message(message_id=1, chat_id=42, user_id=42, text="hi", date=1761500000, chat=None):
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "from": {"id": user_id, "first_name": "Ana", "last_name": "Silva"},
            "chat": chat or {"id": chat_id, "type": "private", "first_name": "Ana", "last_name": "Silva"},
            "date": date,
            "text": text,
        },
    }


def slack_changed(channel="C1", ts="1761500000.000100", text="hello (edited)", edit_ts="1761500100.000000", **message):
    """A ``message_changed`` event: the edited message is nested under ``message``."""
    return {
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "message",
            "subtype": "message_changed",
            "channel": channel,
            "ts": edit_ts,
            "event_ts": edit_ts,
            "message": {"type": "message", "user": "U1", "text": text, "ts": ts,
                        "edited": {"user": "U1", "ts": edit_ts}, **message},
            "previous_message": {"type": "message", "user": "U1", "text": "hello", "ts": ts},
        },
    }


def slack_deleted(channel="C1", deleted_ts="1761500000.000100", ts="1761500200.000000"):
    return {
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "message",
            "subtype": "message_deleted",
            "channel": channel,
            "deleted_ts": deleted_ts,
            "ts": ts,
            "event_ts": ts,
            "previous_message": {"type": "message", "user": "U1", "text": "hello", "ts": deleted_ts},
        },
    }


def telegram_edited(message_id=1, chat_id=42, user_id=42, text="hi (edited)", date=1761500000, edit_date=1761500100):
    return {
        "update_id": message_id + 1000,
        "edited_message": {
            "message_id": message_id,
            "from": {"id": user_id, "first_name": "Ana", "last_name": "Silva"},
            "chat": {"id": chat_id, "type": "private", "first_name": "Ana", "last_name": "Silva"},
            "date": date,
            "edit_date": edit_date,
            "text": text,
        },
    }


def counts(**kwargs):
    """Expected webhook response: every counter, defaulting to zero."""
    return {"stored": 0, "duplicates": 0, "edited": 0, "deleted": 0, "unmatched": 0, **kwargs}
