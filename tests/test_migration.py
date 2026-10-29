"""Opening a database created by an older release must upgrade it in place
and keep its data usable, including by the newer features."""

import os
import sqlite3
import tempfile
import time
import unittest

from aiochat import create_app
from aiochat.db import get_db
from tests.helpers import API_TOKEN, TEST_CONFIG, slack_message

# The first release's schema: no people table, and none of the columns that
# db._ADDED_COLUMNS adds (credential, next_attempt_at, edited_at, deleted_at,
# person_id).
FIRST_RELEASE_SCHEMA = """
CREATE TABLE channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL, name TEXT NOT NULL, external_account_id TEXT NOT NULL,
    webhook_token TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (platform, external_account_id)
);
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels (id) ON DELETE CASCADE,
    external_user_id TEXT NOT NULL, display_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (channel_id, external_user_id)
);
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels (id) ON DELETE CASCADE,
    external_conversation_id TEXT NOT NULL, title TEXT NOT NULL, last_message_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (channel_id, external_conversation_id)
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    contact_id INTEGER REFERENCES contacts (id) ON DELETE SET NULL,
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    external_message_id TEXT, body TEXT NOT NULL, sent_at TEXT NOT NULL, read_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE UNIQUE INDEX messages_external_unique
    ON messages (conversation_id, external_message_id) WHERE external_message_id IS NOT NULL;
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL UNIQUE REFERENCES messages (id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


class LegacyDatabaseTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(FIRST_RELEASE_SCHEMA)
        conn.execute(
            "INSERT INTO channels (platform, name, external_account_id, webhook_token)"
            " VALUES ('slack', 'Old', 'T-OLD', 'tok'), ('telegram', 'Old bot', 'bot-old', 'tok2')"
        )
        conn.execute(
            "INSERT INTO contacts (channel_id, external_user_id, display_name)"
            " VALUES (1, 'U-MIA', 'Mia'), (2, '9002', 'Mia Park')"
        )
        conn.execute(
            "INSERT INTO conversations (channel_id, external_conversation_id, title, last_message_at)"
            " VALUES (1, 'C1', 'general', '2025-10-26T17:33:20.000Z'),"
            "        (2, '9002', 'Mia Park', '2025-10-26T17:35:00.000Z')"
        )
        conn.execute(
            "INSERT INTO messages (conversation_id, contact_id, direction, external_message_id, body, sent_at)"
            " VALUES (1, 1, 'inbound', '1761500000.000100', 'old slack message', '2025-10-26T17:33:20.000Z'),"
            "        (2, 2, 'inbound', '1', 'old telegram message', '2025-10-26T17:35:00.000Z')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        for _ in range(50):
            try:
                os.unlink(self.db_path)
                break
            except PermissionError:
                time.sleep(0.02)

    def open_app(self):
        app = create_app({**TEST_CONFIG, "DATABASE": self.db_path})
        return app, app.test_client()

    def api(self, client, method, path, **kwargs):
        return client.open(path, method=method, headers={"Authorization": f"Bearer {API_TOKEN}"}, **kwargs)

    def test_columns_and_tables_are_added(self):
        app, _ = self.open_app()
        with app.app_context():
            db = get_db()
            columns = lambda table: {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
            self.assertIn("credential", columns("channels"))
            self.assertIn("next_attempt_at", columns("outbox"))
            self.assertLessEqual({"edited_at", "deleted_at"}, columns("messages"))
            self.assertIn("person_id", columns("contacts"))
            self.assertEqual(columns("people"), {"id", "name", "created_at"})
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            # Existing rows survive and start ungrouped.
            self.assertEqual(
                [r["person_id"] for r in db.execute("SELECT person_id FROM contacts ORDER BY id")],
                [None, None],
            )

    def test_opening_twice_is_idempotent(self):
        self.open_app()
        app, client = self.open_app()
        self.assertEqual(self.api(client, "GET", "/api/contacts").get_json()["total"], 2)

    def test_old_contacts_can_be_grouped(self):
        app, client = self.open_app()
        contacts = self.api(client, "GET", "/api/contacts").get_json()["items"]
        self.assertEqual([c["display_name"] for c in contacts], ["Mia", "Mia Park"])
        resp = self.api(client, "POST", "/api/people",
                        json={"name": "Mia", "contact_ids": [c["id"] for c in contacts]})
        self.assertEqual(resp.status_code, 201, resp.get_json())
        person = resp.get_json()
        self.assertEqual(person["unread_count"], 2)

        timeline = self.api(client, "GET", f"/api/people/{person['id']}/timeline").get_json()
        self.assertEqual([m["body"] for m in timeline["items"]], ["old telegram message", "old slack message"])
        self.assertEqual(self.api(client, "GET", f"/api/conversations?person_id={person['id']}").get_json()["total"], 2)
        self.assertEqual(self.api(client, "GET", f"/api/messages?q=old&match=word&person_id={person['id']}").get_json()["total"], 2)

        # The foreign key added by the migration is enforced, including ON DELETE SET NULL.
        self.assertEqual(self.api(client, "DELETE", f"/api/people/{person['id']}").status_code, 204)
        self.assertEqual(self.api(client, "GET", "/api/contacts?unlinked=1").get_json()["total"], 2)

    def test_old_database_still_ingests_and_searches(self):
        app, client = self.open_app()
        payload = slack_message(channel="C1", user="U-MIA", text="fresh", ts="1761500999.000100")
        resp = client.post("/api/webhooks/slack/1", json=payload, headers={"X-Webhook-Token": "tok"})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()["stored"], 1)
        self.assertEqual(self.api(client, "GET", "/api/messages?q=fresh&match=prefix").get_json()["total"], 1)


if __name__ == "__main__":
    unittest.main()
