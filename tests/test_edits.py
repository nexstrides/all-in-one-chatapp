"""Edits and deletions reported by the platforms: stored text, visibility,
and the effect on conversation previews, unread counts and ordering."""

import unittest

from aiochat.services.inbox import purge_deleted
from tests.helpers import (
    AppTestCase,
    counts,
    slack_changed,
    slack_deleted,
    slack_message,
    telegram_edited,
    telegram_message,
)

T0 = "1761500000.000100"  # 2025-10-26T17:33:20Z
T1 = "1761500050.000100"
T2 = "1761500100.000100"


class EditDeleteTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.slack = self.create_channel("slack", "Acme", "T1")
        self.telegram = self.create_channel("telegram", "Bot", "bot-1")

    def conversation(self, channel=None):
        channel = channel or self.slack
        [conv] = self.api("GET", f"/api/conversations?channel_id={channel['id']}").get_json()["items"]
        return conv

    def messages(self, conv_id, query=""):
        return self.api("GET", f"/api/conversations/{conv_id}/messages{query}").get_json()["items"]

    def webhook(self, channel, payload):
        resp = self.send_webhook(channel, payload)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()

    def db_execute(self, sql, params=()):
        with self.app.app_context():
            from aiochat.db import get_db
            get_db().execute(sql, params)
            get_db().commit()


class SlackEditTests(EditDeleteTestCase):
    def test_edit_updates_stored_text(self):
        self.webhook(self.slack, slack_message(text="helo", ts=T0))
        self.assertEqual(self.webhook(self.slack, slack_changed(ts=T0, text="hello")), counts(edited=1))
        [msg] = self.messages(self.conversation()["id"])
        self.assertEqual(msg["body"], "hello")
        self.assertEqual(msg["edited_at"], "2025-10-26T17:35:00.000Z")
        self.assertIsNone(msg["deleted_at"])
        # Still one message: the edit did not create a new row.
        self.assertEqual(self.conversation()["last_message_preview"], "hello")

    def test_edit_keeps_read_state_and_unread_count(self):
        self.webhook(self.slack, slack_message(text="one", ts=T0))
        self.webhook(self.slack, slack_message(text="two", ts=T1))
        conv_id = self.conversation()["id"]
        older = min(self.messages(conv_id), key=lambda m: m["id"])
        self.api("POST", f"/api/conversations/{conv_id}/read", json={"up_to_message_id": older["id"]})
        self.assertEqual(self.conversation()["unread_count"], 1)

        self.webhook(self.slack, slack_changed(ts=T0, text="one!"))  # the read one
        self.webhook(self.slack, slack_changed(ts=T1, text="two!"))  # the unread one
        self.assertEqual(self.conversation()["unread_count"], 1, "edits never re-flag as unread")
        by_body = {m["body"]: m for m in self.messages(conv_id)}
        self.assertIsNotNone(by_body["one!"]["read_at"])
        self.assertIsNone(by_body["two!"]["read_at"])

    def test_edit_does_not_reorder_conversations(self):
        self.webhook(self.slack, slack_message(channel="C1", text="older", ts=T0))
        self.webhook(self.slack, slack_message(channel="C2", text="newer", ts=T1))
        # Editing the older conversation's message long after must not move it up.
        self.webhook(self.slack, slack_changed(channel="C1", ts=T0, text="older, edited", edit_ts="1761509999.000000"))
        items = self.api("GET", "/api/conversations").get_json()["items"]
        self.assertEqual([c["title"] for c in items], ["C2", "C1"])
        self.assertEqual(items[1]["last_message_preview"], "older, edited")
        self.assertTrue(items[1]["last_message_at"].startswith("2025-10-26T17:33:20"))

    def test_edit_of_older_message_does_not_change_preview(self):
        self.webhook(self.slack, slack_message(text="first", ts=T0))
        self.webhook(self.slack, slack_message(text="second", ts=T1))
        self.webhook(self.slack, slack_changed(ts=T0, text="first, edited"))
        self.assertEqual(self.conversation()["last_message_preview"], "second")

    def test_edit_with_unchanged_text_is_a_noop(self):
        """Slack sends message_changed for link unfurls too; same text is not an edit."""
        self.webhook(self.slack, slack_message(text="see https://example.com", ts=T0))
        result = self.webhook(self.slack, slack_changed(ts=T0, text="see https://example.com"))
        self.assertEqual(result, counts(duplicates=1))
        [msg] = self.messages(self.conversation()["id"])
        self.assertIsNone(msg["edited_at"])

    def test_edit_of_unknown_message_is_unmatched(self):
        self.webhook(self.slack, slack_message(text="x", ts=T0))
        self.assertEqual(self.webhook(self.slack, slack_changed(ts="1700000000.000001")), counts(unmatched=1))
        self.assertEqual(self.webhook(self.slack, slack_changed(channel="C9", ts=T0)), counts(unmatched=1))
        [msg] = self.messages(self.conversation()["id"])
        self.assertEqual(msg["body"], "x")

    def test_edited_text_is_searchable(self):
        self.webhook(self.slack, slack_message(text="budget", ts=T0))
        self.webhook(self.slack, slack_changed(ts=T0, text="forecast"))
        self.assertEqual(self.api("GET", "/api/messages?q=budget").get_json()["total"], 0)
        self.assertEqual(self.api("GET", "/api/messages?q=forecast").get_json()["total"], 1)


class TelegramEditTests(EditDeleteTestCase):
    def test_edit_updates_stored_text(self):
        self.webhook(self.telegram, telegram_message(message_id=1, text="teh typo", date=1761500000))
        result = self.webhook(self.telegram, telegram_edited(message_id=1, text="the typo", edit_date=1761500100))
        self.assertEqual(result, counts(edited=1))
        conv = self.conversation(self.telegram)
        [msg] = self.messages(conv["id"])
        self.assertEqual(msg["body"], "the typo")
        self.assertEqual(msg["edited_at"], "2025-10-26T17:35:00.000Z")
        self.assertEqual(msg["sender"]["display_name"], "Ana Silva")
        self.assertEqual(conv["last_message_preview"], "the typo")
        self.assertEqual(conv["unread_count"], 1)

    def test_edit_is_scoped_to_the_chat(self):
        self.webhook(self.telegram, telegram_message(message_id=1, chat_id=42, text="a", date=1761500000))
        self.webhook(self.telegram, telegram_message(message_id=1, chat_id=43, user_id=43, text="b", date=1761500001))
        # Telegram message ids are per chat; an edit in chat 43 must not touch chat 42.
        self.webhook(self.telegram, telegram_edited(message_id=1, chat_id=43, text="b!"))
        convs = self.api("GET", f"/api/conversations?channel_id={self.telegram['id']}").get_json()["items"]
        bodies = sorted(m["body"] for c in convs for m in self.messages(c["id"]))
        self.assertEqual(bodies, ["a", "b!"])


class DeleteTests(EditDeleteTestCase):
    def test_delete_hides_but_keeps_the_message(self):
        self.webhook(self.slack, slack_message(text="oops", ts=T0))
        self.webhook(self.slack, slack_message(text="fine", ts=T1))
        conv_id = self.conversation()["id"]
        self.assertEqual(self.webhook(self.slack, slack_deleted(deleted_ts=T0)), counts(deleted=1))

        visible = self.api("GET", f"/api/conversations/{conv_id}/messages").get_json()
        self.assertEqual(visible["total"], 1)
        self.assertEqual([m["body"] for m in visible["items"]], ["fine"])

        # History is kept: the row is still there with deleted_at set.
        everything = self.api("GET", f"/api/conversations/{conv_id}/messages?include_deleted=1").get_json()
        self.assertEqual(everything["total"], 2)
        deleted = next(m for m in everything["items"] if m["body"] == "oops")
        self.assertEqual(deleted["deleted_at"], "2025-10-26T17:36:40.000Z")

    def test_delete_updates_preview_unread_and_ordering(self):
        self.webhook(self.slack, slack_message(channel="C1", text="c1 first", ts=T0))
        self.webhook(self.slack, slack_message(channel="C2", text="c2 only", ts=T1))
        self.webhook(self.slack, slack_message(channel="C1", text="c1 latest", ts=T2))
        items = self.api("GET", "/api/conversations").get_json()["items"]
        self.assertEqual([c["title"] for c in items], ["C1", "C2"])
        self.assertEqual(items[0]["unread_count"], 2)

        # Deleting C1's newest message: preview and last_message_at fall back
        # to the remaining message, so C2 is now the most recent conversation.
        self.webhook(self.slack, slack_deleted(channel="C1", deleted_ts=T2))
        items = self.api("GET", "/api/conversations").get_json()["items"]
        self.assertEqual([c["title"] for c in items], ["C2", "C1"])
        c1 = items[1]
        self.assertEqual(c1["last_message_preview"], "c1 first")
        self.assertTrue(c1["last_message_at"].startswith("2025-10-26T17:33:20"))
        self.assertEqual(c1["unread_count"], 1)

    def test_deleting_the_only_message_empties_the_conversation(self):
        self.webhook(self.slack, slack_message(text="gone", ts=T0))
        self.webhook(self.slack, slack_deleted(deleted_ts=T0))
        conv = self.conversation()
        self.assertIsNone(conv["last_message_preview"])
        self.assertIsNone(conv["last_message_at"])
        self.assertEqual(conv["unread_count"], 0)
        self.assertEqual(self.api("GET", "/api/conversations?unread=1").get_json()["total"], 0)
        self.assertEqual(self.messages(conv["id"]), [])

    def test_deleted_message_is_not_searchable_and_not_marked_read(self):
        self.webhook(self.slack, slack_message(text="secret plan", ts=T0))
        self.webhook(self.slack, slack_message(text="public", ts=T1))
        conv_id = self.conversation()["id"]
        self.webhook(self.slack, slack_deleted(deleted_ts=T0))
        self.assertEqual(self.api("GET", "/api/messages?q=secret").get_json()["total"], 0)
        # mark-all-read only counts visible messages.
        resp = self.api("POST", f"/api/conversations/{conv_id}/read")
        self.assertEqual(resp.get_json(), {"updated": 1})

    def test_redelivery_after_delete_stays_deleted(self):
        """Slack may retry the original event after the delete; it must not resurrect."""
        self.webhook(self.slack, slack_message(text="oops", ts=T0))
        self.webhook(self.slack, slack_deleted(deleted_ts=T0))
        self.assertEqual(self.webhook(self.slack, slack_message(text="oops", ts=T0)), counts(duplicates=1))
        self.assertEqual(self.messages(self.conversation()["id"]), [])

    def test_delete_then_edit_or_delete_again(self):
        self.webhook(self.slack, slack_message(text="oops", ts=T0))
        self.webhook(self.slack, slack_deleted(deleted_ts=T0))
        self.assertEqual(self.webhook(self.slack, slack_deleted(deleted_ts=T0)), counts(duplicates=1))
        self.assertEqual(self.webhook(self.slack, slack_changed(ts=T0, text="late edit")), counts(duplicates=1))
        [msg] = self.messages(self.conversation()["id"], "?include_deleted=1")
        self.assertEqual(msg["body"], "oops")

    def test_delete_of_unknown_message_is_unmatched(self):
        self.webhook(self.slack, slack_message(text="x", ts=T0))
        self.assertEqual(self.webhook(self.slack, slack_deleted(deleted_ts="1700000000.000001")), counts(unmatched=1))

    def test_tombstone_deletes_thread_parent(self):
        self.webhook(self.slack, slack_message(text="parent", ts=T0))
        result = self.webhook(self.slack, slack_changed(ts=T0, subtype="tombstone", text="This message was deleted."))
        self.assertEqual(result, counts(deleted=1))
        self.assertEqual(self.messages(self.conversation()["id"]), [])

    def test_delete_of_our_own_reply(self):
        """A user deleting the bot's message in Slack hides it here too."""
        self.webhook(self.slack, slack_message(text="hi", ts=T0))
        conv_id = self.conversation()["id"]
        reply = self.api("POST", f"/api/conversations/{conv_id}/messages", json={"body": "hello"}).get_json()
        self.db_execute("UPDATE messages SET external_message_id = ? WHERE id = ?", ("1761500300.000100", reply["id"]))
        self.assertEqual(self.webhook(self.slack, slack_deleted(deleted_ts="1761500300.000100")), counts(deleted=1))
        self.assertEqual([m["body"] for m in self.messages(conv_id)], ["hi"])
        self.assertEqual(self.conversation()["last_message_preview"], "hi")


class RetentionTests(EditDeleteTestCase):
    def setUp(self):
        super().setUp()
        self.webhook(self.slack, slack_message(text="old", ts=T0))
        self.webhook(self.slack, slack_message(text="recent", ts=T1))
        self.webhook(self.slack, slack_message(text="live", ts=T2))
        self.webhook(self.slack, slack_deleted(deleted_ts=T0))
        self.webhook(self.slack, slack_deleted(deleted_ts=T1))
        self.conv_id = self.conversation()["id"]
        # deleted_at comes from the platform's event time, which in these
        # fixtures is fixed in the past; pin "old" to long ago and "recent"
        # to a moment ago so the test does not depend on today's date.
        from aiochat.services.inbox import utc_now_iso
        self.db_execute("UPDATE messages SET deleted_at = '2020-01-01T00:00:00.000Z' WHERE body = 'old'")
        self.db_execute("UPDATE messages SET deleted_at = ? WHERE body = 'recent'", (utc_now_iso(),))

    def all_bodies(self):
        return sorted(m["body"] for m in self.messages(self.conv_id, "?include_deleted=1"))

    def test_purge_removes_only_expired_deletions(self):
        self.app.config["DELETED_RETENTION_DAYS"] = 30
        with self.app.app_context():
            self.assertEqual(purge_deleted(), 1)
        self.assertEqual(self.all_bodies(), ["live", "recent"])
        with self.app.app_context():
            self.assertEqual(purge_deleted(), 0)

    def test_retention_zero_keeps_forever(self):
        self.app.config["DELETED_RETENTION_DAYS"] = 0
        with self.app.app_context():
            self.assertEqual(purge_deleted(), 0)
        self.assertEqual(self.all_bodies(), ["live", "old", "recent"])

    def test_explicit_retention_argument(self):
        with self.app.app_context():
            self.assertEqual(purge_deleted(retention_days=1), 1)
        self.assertEqual(self.all_bodies(), ["live", "recent"])

    def test_worker_pass_purges(self):
        from aiochat.worker import OutboxWorker
        self.app.config["DELETED_RETENTION_DAYS"] = 30
        OutboxWorker(self.app, interval=60).run_once()
        self.assertEqual(self.all_bodies(), ["live", "recent"])

    def test_cli_command(self):
        self.app.config["DELETED_RETENTION_DAYS"] = 30
        result = self.app.test_cli_runner().invoke(args=["purge-deleted"])
        self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))
        self.assertIn("removed=1", result.output)


if __name__ == "__main__":
    unittest.main()
