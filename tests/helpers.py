"""Shared test setup: a fresh app and database per test."""

import os
import tempfile
import unittest

from aiochat import create_app

API_TOKEN = "test-token"


class AppTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.app = create_app({"TESTING": True, "DATABASE": self.db_path, "API_TOKEN": API_TOKEN})
        self.client = self.app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)

    # -- HTTP helpers -----------------------------------------------------

    def api(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {})
        headers.setdefault("Authorization", f"Bearer {API_TOKEN}")
        return self.client.open(path, method=method, headers=headers, **kwargs)

    def create_channel(self, platform="slack", name="Acme", account="T1"):
        resp = self.api(
            "POST",
            "/api/channels",
            json={"platform": platform, "name": name, "external_account_id": account},
        )
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
