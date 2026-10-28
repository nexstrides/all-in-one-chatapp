import unittest

from tests.helpers import (
    AppTestCase,
    counts,
    slack_changed,
    slack_deleted,
    slack_message,
    telegram_edited,
    telegram_message,
)


class HealthAndAuthTests(AppTestCase):
    def test_health_needs_no_token(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})

    def test_api_requires_token(self):
        resp = self.client.get("/api/conversations")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()["error"]["code"], "unauthorized")

        resp = self.api("GET", "/api/conversations", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(resp.status_code, 401)

    def test_unknown_route_returns_json_error(self):
        resp = self.api("GET", "/api/nope")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("error", resp.get_json())


class ChannelTests(AppTestCase):
    def test_create_returns_token_once(self):
        channel = self.create_channel()
        self.assertIn("webhook_token", channel)

        listed = self.api("GET", "/api/channels").get_json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertNotIn("webhook_token", listed[0])

        fetched = self.api("GET", f"/api/channels/{channel['id']}").get_json()
        self.assertNotIn("webhook_token", fetched)

    def test_rejects_unknown_platform_and_missing_fields(self):
        resp = self.api(
            "POST", "/api/channels",
            json={"platform": "fax", "name": "x", "external_account_id": "y"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "unsupported_platform")

        resp = self.api("POST", "/api/channels", json={"platform": "slack"})
        self.assertEqual(resp.status_code, 400)

    def test_credential_is_write_only(self):
        channel = self.create_channel(credential="xoxb-secret")
        self.assertTrue(channel["can_send"])
        self.assertNotIn("credential", channel)
        self.assertNotIn("xoxb-secret", self.api("GET", "/api/channels").get_data(as_text=True))

        resp = self.api("PATCH", f"/api/channels/{channel['id']}", json={"credential": None, "name": "Renamed"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["name"], "Renamed")
        self.assertFalse(resp.get_json()["can_send"])
        self.assertEqual(self.api("PATCH", f"/api/channels/{channel['id']}", json={"name": ""}).status_code, 400)
        self.assertEqual(self.api("PATCH", "/api/channels/999", json={"name": "x"}).status_code, 404)

    def test_duplicate_account_conflicts(self):
        self.create_channel()
        resp = self.api(
            "POST", "/api/channels",
            json={"platform": "slack", "name": "Again", "external_account_id": "T1"},
        )
        self.assertEqual(resp.status_code, 409)

    def test_delete_removes_conversations(self):
        channel = self.create_channel()
        self.send_webhook(channel, slack_message())
        self.assertEqual(self.api("DELETE", f"/api/channels/{channel['id']}").status_code, 204)
        self.assertEqual(self.api("GET", "/api/conversations").get_json()["total"], 0)
        self.assertEqual(self.api("GET", f"/api/channels/{channel['id']}").status_code, 404)


class WebhookTests(AppTestCase):
    def test_slack_handshake(self):
        channel = self.create_channel()
        resp = self.send_webhook(channel, {"type": "url_verification", "challenge": "c-123"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"challenge": "c-123"})

    def test_rejects_wrong_token(self):
        channel = self.create_channel()
        resp = self.send_webhook(channel, slack_message(), token="nope")
        self.assertEqual(resp.status_code, 401)

    def test_token_in_query_string(self):
        """Slack can only be given a URL, so the token may ride in it."""
        channel = self.create_channel()
        resp = self.client.post(
            f"/api/webhooks/slack/{channel['id']}?token={channel['webhook_token']}",
            json=slack_message(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["stored"], 1)
        resp = self.client.post(f"/api/webhooks/slack/{channel['id']}?token=wrong", json=slack_message())
        self.assertEqual(resp.status_code, 401)

    def test_token_in_telegram_header(self):
        channel = self.create_channel("telegram", "Bot", "bot-1")
        resp = self.client.post(
            f"/api/webhooks/telegram/{channel['id']}",
            json=telegram_message(),
            headers={"X-Telegram-Bot-Api-Secret-Token": channel["webhook_token"]},
        )
        self.assertEqual(resp.status_code, 200)

    def test_missing_token_is_rejected(self):
        channel = self.create_channel()
        resp = self.client.post(f"/api/webhooks/slack/{channel['id']}", json=slack_message())
        self.assertEqual(resp.status_code, 401)

    def test_platform_must_match_channel(self):
        channel = self.create_channel()
        resp = self.client.post(
            f"/api/webhooks/telegram/{channel['id']}",
            json=telegram_message(),
            headers={"X-Webhook-Token": channel["webhook_token"]},
        )
        self.assertEqual(resp.status_code, 404)

    def test_malformed_payload_is_400(self):
        channel = self.create_channel()
        resp = self.send_webhook(channel, slack_message(ts="bad"))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "invalid_payload")

    def test_redelivery_is_deduplicated(self):
        channel = self.create_channel()
        first = self.send_webhook(channel, slack_message()).get_json()
        second = self.send_webhook(channel, slack_message()).get_json()
        self.assertEqual(first, counts(stored=1))
        self.assertEqual(second, counts(duplicates=1))

    def test_ignored_events_store_nothing(self):
        channel = self.create_channel()
        resp = self.send_webhook(channel, slack_message(subtype="bot_message"))
        self.assertEqual(resp.get_json(), counts())


class InboxTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.slack = self.create_channel("slack", "Acme", "T1")
        self.telegram = self.create_channel("telegram", "Bot", "bot-1")

    def conversations(self, query=""):
        return self.api("GET", f"/api/conversations{query}").get_json()

    def test_conversations_from_all_platforms_newest_first(self):
        self.send_webhook(self.slack, slack_message(channel="C1", ts="1761500000.000100"))
        self.send_webhook(self.telegram, telegram_message(date=1761500100))
        self.send_webhook(self.slack, slack_message(channel="C2", ts="1761500050.000100"))

        items = self.conversations()["items"]
        self.assertEqual([c["platform"] for c in items], ["telegram", "slack", "slack"])
        self.assertEqual(items[1]["title"], "C2")

    def test_out_of_order_delivery_keeps_latest_timestamp(self):
        self.send_webhook(self.slack, slack_message(text="newer", ts="1761500100.000100"))
        self.send_webhook(self.slack, slack_message(text="older", ts="1761500000.000100"))
        [conv] = self.conversations()["items"]
        self.assertEqual(conv["last_message_preview"], "newer")
        self.assertTrue(conv["last_message_at"].startswith("2025-10-26T17:35"))

    def test_unread_count_and_mark_read(self):
        for i in range(3):
            self.send_webhook(self.telegram, telegram_message(message_id=i + 1, date=1761500000 + i))
        [conv] = self.conversations()["items"]
        self.assertEqual(conv["unread_count"], 3)

        messages = self.api("GET", f"/api/conversations/{conv['id']}/messages").get_json()["items"]
        oldest_two = sorted(m["id"] for m in messages)[1]
        resp = self.api("POST", f"/api/conversations/{conv['id']}/read",
                        json={"up_to_message_id": oldest_two})
        self.assertEqual(resp.get_json(), {"updated": 2})
        self.assertEqual(self.conversations()["items"][0]["unread_count"], 1)

        resp = self.api("POST", f"/api/conversations/{conv['id']}/read")
        self.assertEqual(resp.get_json(), {"updated": 1})
        self.assertEqual(self.conversations("?unread=1")["total"], 0)

    def test_filter_by_channel(self):
        self.send_webhook(self.slack, slack_message())
        self.send_webhook(self.telegram, telegram_message())
        result = self.conversations(f"?channel_id={self.telegram['id']}")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["platform"], "telegram")

    def test_messages_paginate_newest_first(self):
        for i in range(5):
            self.send_webhook(
                self.telegram,
                telegram_message(message_id=i + 1, text=f"m{i}", date=1761500000 + i),
            )
        conv_id = self.conversations()["items"][0]["id"]
        page1 = self.api("GET", f"/api/conversations/{conv_id}/messages?page_size=2").get_json()
        page3 = self.api("GET", f"/api/conversations/{conv_id}/messages?page_size=2&page=3").get_json()
        self.assertEqual(page1["total"], 5)
        self.assertEqual([m["body"] for m in page1["items"]], ["m4", "m3"])
        self.assertEqual([m["body"] for m in page3["items"]], ["m0"])
        self.assertEqual(page1["items"][0]["sender"]["display_name"], "Ana Silva")

    def test_bad_pagination_params(self):
        for query in ("?page=0", "?page=abc", "?page_size=-1"):
            self.assertEqual(self.api("GET", f"/api/conversations{query}").status_code, 400, query)
        big = self.conversations("?page_size=1000")
        self.assertEqual(big["page_size"], 100)

    def test_reply_is_queued_and_becomes_preview(self):
        self.send_webhook(self.telegram, telegram_message(text="ping"))
        conv_id = self.conversations()["items"][0]["id"]

        resp = self.api("POST", f"/api/conversations/{conv_id}/messages", json={"body": " pong "})
        self.assertEqual(resp.status_code, 201)
        msg = resp.get_json()
        self.assertEqual(msg["direction"], "outbound")
        self.assertEqual(msg["body"], "pong")
        self.assertEqual(msg["delivery_status"], "pending")
        self.assertIsNone(msg["sender"])

        [conv] = self.conversations()["items"]
        self.assertEqual(conv["last_message_preview"], "pong")
        self.assertEqual(conv["unread_count"], 1)  # replies never count as unread

    def test_reply_validation(self):
        self.send_webhook(self.telegram, telegram_message())
        conv_id = self.conversations()["items"][0]["id"]
        self.assertEqual(
            self.api("POST", f"/api/conversations/{conv_id}/messages", json={"body": "  "}).status_code,
            400,
        )
        self.assertEqual(
            self.api("POST", f"/api/conversations/{conv_id}/messages", json={"body": "x" * 4001}).status_code,
            400,
        )
        self.assertEqual(
            self.api("POST", "/api/conversations/999/messages", json={"body": "hi"}).status_code,
            404,
        )

    def test_search_across_platforms(self):
        self.send_webhook(self.slack, slack_message(text="Budget review at 3", ts="1761500000.000100"))
        self.send_webhook(self.telegram, telegram_message(text="did you see the BUDGET?", date=1761500100))
        self.send_webhook(self.telegram, telegram_message(message_id=2, text="unrelated", date=1761500200))
        conv_id = self.conversations(f"?channel_id={self.telegram['id']}")["items"][0]["id"]
        self.api("POST", f"/api/conversations/{conv_id}/messages", json={"body": "budget looks fine"})

        result = self.api("GET", "/api/messages?q=budget").get_json()
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["query"], "budget")
        self.assertEqual([m["direction"] for m in result["items"]], ["outbound", "inbound", "inbound"])
        self.assertEqual(
            [m["conversation"]["platform"] for m in result["items"]], ["telegram", "telegram", "slack"]
        )
        self.assertEqual(result["items"][2]["conversation"]["title"], "C1")
        self.assertEqual(result["items"][1]["sender"]["display_name"], "Ana Silva")

        paged = self.api("GET", "/api/messages?q=budget&page_size=2&page=2").get_json()
        self.assertEqual(len(paged["items"]), 1)

    def test_search_escapes_like_wildcards(self):
        self.send_webhook(self.telegram, telegram_message(text="100% done", date=1761500000))
        self.send_webhook(self.telegram, telegram_message(message_id=2, text="100 done", date=1761500001))
        self.assertEqual(self.api("GET", "/api/messages?q=100%25").get_json()["total"], 1)
        self.assertEqual(self.api("GET", "/api/messages?q=_").get_json()["total"], 0)

    def test_search_requires_query(self):
        self.assertEqual(self.api("GET", "/api/messages").status_code, 400)
        self.assertEqual(self.api("GET", "/api/messages?q=%20").status_code, 400)


if __name__ == "__main__":
    unittest.main()
