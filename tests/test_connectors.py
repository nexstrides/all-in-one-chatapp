import unittest

from aiochat.connectors import CONNECTORS, PayloadError
from aiochat.connectors.base import iso_from_epoch
from tests.helpers import slack_message, telegram_message


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

    def test_display_name_falls_back_to_user_id(self):
        [msg] = self.slack.parse(slack_message(user="U9"))
        self.assertEqual(msg.user_display_name, "U9")

    def test_ignores_subtypes_and_other_events(self):
        self.assertEqual(self.slack.parse(slack_message(subtype="message_changed")), [])
        self.assertEqual(self.slack.parse({"type": "event_callback", "event": {"type": "reaction_added"}}), [])
        self.assertEqual(self.slack.parse({"type": "app_rate_limited"}), [])

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

    def test_rejects_bad_date(self):
        payload = telegram_message()
        payload["message"]["date"] = "yesterday"
        with self.assertRaises(PayloadError):
            self.telegram.parse(payload)


if __name__ == "__main__":
    unittest.main()
