import unittest

from aiochat.connectors import CONNECTORS, InboundDelete, InboundEdit, PayloadError
from aiochat.connectors.base import iso_from_epoch
from tests.helpers import slack_changed, slack_deleted, slack_message, telegram_edited, telegram_message


class IsoFromEpochTests(unittest.TestCase):
    def test_formats_utc_with_milliseconds(self):
        self.assertEqual(iso_from_epoch(0), "1970-01-01T00:00:00.000Z")
        self.assertEqual(iso_from_epoch(1.25), "1970-01-01T00:00:01.250Z")


class SlackConnectorTests(unittest.TestCase):
    def setUp(self):
        self.slack = CONNECTORS["slack"]

    def test_url_verification_handshake(self):
        self.assertEqual(
            self.slack.handshake({"type": "url_verification", "challenge": "abc"}),
            {"challenge": "abc"},
        )
        self.assertIsNone(self.slack.handshake(slack_message()))

    def test_parses_plain_message(self):
        [msg] = self.slack.parse(
            slack_message(text="hey", user_profile={"display_name": "Mia"}, channel_name="general")
        )
        self.assertEqual(msg.external_conversation_id, "C1")
        self.assertEqual(msg.conversation_title, "general")
        self.assertEqual(msg.user_display_name, "Mia")
        self.assertEqual(msg.external_message_id, "1761500000.000100")
        self.assertEqual(msg.body, "hey")
        self.assertTrue(msg.sent_at.endswith("Z"))

    def test_direct_message_is_titled_after_the_person(self):
        [msg] = self.slack.parse(
            slack_message(channel="D1", channel_type="im", user_profile={"real_name": "Mia Park"})
        )
        self.assertEqual(msg.conversation_title, "Mia Park")
        [msg] = self.slack.parse(slack_message(channel="C1", channel_type="channel"))
        self.assertEqual(msg.conversation_title, "C1")

    def test_display_name_falls_back_to_user_id(self):
        [msg] = self.slack.parse(slack_message(user="U9"))
        self.assertEqual(msg.user_display_name, "U9")

    def test_ignores_other_subtypes_and_events(self):
        self.assertEqual(self.slack.parse(slack_message(subtype="bot_message")), [])
        self.assertEqual(self.slack.parse(slack_message(subtype="channel_join")), [])
        self.assertEqual(self.slack.parse({"type": "event_callback", "event": {"type": "reaction_added"}}), [])
        self.assertEqual(self.slack.parse({"type": "app_rate_limited"}), [])

    def test_parses_message_changed(self):
        [edit] = self.slack.parse(slack_changed(channel="C7", ts="1761500000.000100", text="new text"))
        self.assertIsInstance(edit, InboundEdit)
        self.assertEqual(edit.external_conversation_id, "C7")
        self.assertEqual(edit.external_message_id, "1761500000.000100")  # the original ts, not the edit's
        self.assertEqual(edit.body, "new text")
        self.assertEqual(edit.edited_at, "2025-10-26T17:35:00.000Z")

    def test_message_changed_falls_back_to_event_ts(self):
        payload = slack_changed(edit_ts="1761500100.000000")
        del payload["event"]["message"]["edited"]
        [edit] = self.slack.parse(payload)
        self.assertEqual(edit.edited_at, "2025-10-26T17:35:00.000Z")

    def test_tombstone_is_a_delete(self):
        [event] = self.slack.parse(slack_changed(subtype="tombstone", text="This message was deleted."))
        self.assertIsInstance(event, InboundDelete)
        self.assertEqual(event.external_message_id, "1761500000.000100")

    def test_parses_message_deleted(self):
        [event] = self.slack.parse(slack_deleted(channel="C7", deleted_ts="1761500000.000100", ts="1761500200.000000"))
        self.assertIsInstance(event, InboundDelete)
        self.assertEqual(event.external_conversation_id, "C7")
        self.assertEqual(event.external_message_id, "1761500000.000100")
        self.assertEqual(event.deleted_at, "2025-10-26T17:36:40.000Z")

    def test_rejects_malformed_edit_and_delete(self):
        payload = slack_changed()
        del payload["event"]["message"]["ts"]
        with self.assertRaises(PayloadError):
            self.slack.parse(payload)
        payload = slack_deleted()
        del payload["event"]["deleted_ts"]
        with self.assertRaises(PayloadError):
            self.slack.parse(payload)
        with self.assertRaises(PayloadError):
            self.slack.parse(slack_deleted(ts="soon"))

    def test_rejects_malformed_message(self):
        payload = slack_message()
        del payload["event"]["user"]
        with self.assertRaises(PayloadError):
            self.slack.parse(payload)
        with self.assertRaises(PayloadError):
            self.slack.parse(slack_message(ts="not-a-number"))


class TelegramConnectorTests(unittest.TestCase):
    def setUp(self):
        self.telegram = CONNECTORS["telegram"]

    def test_parses_private_message(self):
        [msg] = self.telegram.parse(telegram_message(text="ola"))
        self.assertEqual(msg.external_conversation_id, "42")
        self.assertEqual(msg.conversation_title, "Ana Silva")
        self.assertEqual(msg.user_display_name, "Ana Silva")
        self.assertEqual(msg.external_message_id, "1")
        self.assertEqual(msg.body, "ola")

    def test_group_chat_uses_group_title(self):
        [msg] = self.telegram.parse(
            telegram_message(chat={"id": -100, "type": "group", "title": "Book club"})
        )
        self.assertEqual(msg.external_conversation_id, "-100")
        self.assertEqual(msg.conversation_title, "Book club")

    def test_ignores_non_text_updates(self):
        payload = telegram_message()
        del payload["message"]["text"]
        payload["message"]["sticker"] = {"file_id": "x"}
        self.assertEqual(self.telegram.parse(payload), [])
        self.assertEqual(self.telegram.parse({"update_id": 5, "edited_message": {}}), [])

    def test_parses_edited_message(self):
        [edit] = self.telegram.parse(telegram_edited(message_id=3, chat_id=42, text="fixed typo", edit_date=1761500100))
        self.assertIsInstance(edit, InboundEdit)
        self.assertEqual(edit.external_conversation_id, "42")
        self.assertEqual(edit.external_message_id, "3")
        self.assertEqual(edit.body, "fixed typo")
        self.assertEqual(edit.edited_at, "2025-10-26T17:35:00.000Z")

    def test_ignores_edited_message_without_text(self):
        payload = telegram_edited()
        del payload["edited_message"]["text"]
        payload["edited_message"]["caption"] = "new caption"
        self.assertEqual(self.telegram.parse(payload), [])

    def test_rejects_bad_edit_date(self):
        payload = telegram_edited()
        payload["edited_message"]["edit_date"] = "later"
        with self.assertRaises(PayloadError):
            self.telegram.parse(payload)

    def test_rejects_bad_date(self):
        payload = telegram_message()
        payload["message"]["date"] = "yesterday"
        with self.assertRaises(PayloadError):
            self.telegram.parse(payload)


if __name__ == "__main__":
    unittest.main()
