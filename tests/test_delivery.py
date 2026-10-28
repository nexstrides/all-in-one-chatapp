"""Outbound delivery: connectors' send(), the outbox queue and the worker."""

import json
import threading
import unittest
import urllib.error
from unittest import mock

from aiochat.connectors import CONNECTORS, DeliveryError
from aiochat.connectors import http
from aiochat.services import outbox as outbox_service
from aiochat.worker import OutboxWorker
from tests.helpers import AppTestCase, counts, slack_message, telegram_message

POST_JSON = "aiochat.connectors.http.post_json"


class PostJsonTests(unittest.TestCase):
    """The urllib wrapper, exercised with a fake urlopen."""

    def _response(self, status, body):
        resp = mock.MagicMock()
        resp.status = status
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        return resp

    def test_returns_status_and_body(self):
        with mock.patch("urllib.request.urlopen", return_value=self._response(200, b'{"ok": true}')) as urlopen:
            self.assertEqual(http.post_json("https://x/api", {"a": 1}, headers={"Authorization": "Bearer t"}), (200, {"ok": True}))
        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"a": 1})
        self.assertEqual(request.get_header("Authorization"), "Bearer t")
        self.assertTrue(request.get_header("Content-type").startswith("application/json"))

    def test_http_errors_are_returned_not_raised(self):
        err = urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)
        err.read = lambda: b'{"ok": false, "description": "nope"}'
        with mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertEqual(http.post_json("https://x", {}), (403, {"ok": False, "description": "nope"}))

    def test_network_failure_is_retryable(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(DeliveryError) as ctx:
                http.post_json("https://x", {})
        self.assertTrue(ctx.exception.retryable)

    def test_non_json_is_retryable(self):
        with mock.patch("urllib.request.urlopen", return_value=self._response(502, b"<html>bad gateway</html>")):
            with self.assertRaises(DeliveryError) as ctx:
                http.post_json("https://x", {})
        self.assertTrue(ctx.exception.retryable)


class SlackSendTests(unittest.TestCase):
    def setUp(self):
        self.slack = CONNECTORS["slack"]

    def test_posts_message_and_returns_ts(self):
        with mock.patch(POST_JSON, return_value=(200, {"ok": True, "ts": "1761500000.000200"})) as post:
            ts = self.slack.send("xoxb-1", "C1", "hello", timeout=3)
        self.assertEqual(ts, "1761500000.000200")
        url, payload = post.call_args.args
        self.assertEqual(url, "https://slack.com/api/chat.postMessage")
        self.assertEqual(payload, {"channel": "C1", "text": "hello"})
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer xoxb-1"})
        self.assertEqual(post.call_args.kwargs["timeout"], 3)

    def test_api_error_is_permanent(self):
        with mock.patch(POST_JSON, return_value=(200, {"ok": False, "error": "channel_not_found"})):
            with self.assertRaises(DeliveryError) as ctx:
                self.slack.send("xoxb-1", "C1", "hello")
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("channel_not_found", str(ctx.exception))

    def test_rate_limit_and_5xx_are_retryable(self):
        for response in ((429, {"ok": False, "error": "ratelimited"}), (503, {}), (200, {"ok": False, "error": "ratelimited"})):
            with mock.patch(POST_JSON, return_value=response):
                with self.assertRaises(DeliveryError) as ctx:
                    self.slack.send("xoxb-1", "C1", "hello")
            self.assertTrue(ctx.exception.retryable, response)


class TelegramSendTests(unittest.TestCase):
    def setUp(self):
        self.telegram = CONNECTORS["telegram"]

    def test_posts_message_and_returns_message_id(self):
        with mock.patch(POST_JSON, return_value=(200, {"ok": True, "result": {"message_id": 77}})) as post:
            self.assertEqual(self.telegram.send("123:abc", "42", "hi"), "77")
        url, payload = post.call_args.args
        self.assertEqual(url, "https://api.telegram.org/bot123:abc/sendMessage")
        self.assertEqual(payload, {"chat_id": "42", "text": "hi"})

    def test_api_error_is_permanent(self):
        with mock.patch(POST_JSON, return_value=(403, {"ok": False, "description": "Forbidden: bot was blocked"})):
            with self.assertRaises(DeliveryError) as ctx:
                self.telegram.send("123:abc", "42", "hi")
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("blocked", str(ctx.exception))

    def test_token_is_scrubbed_from_errors(self):
        with mock.patch(POST_JSON, side_effect=DeliveryError("network error: https://api.telegram.org/bot123:abc/x", retryable=True)):
            with self.assertRaises(DeliveryError) as ctx:
                self.telegram.send("123:abc", "42", "hi")
        self.assertNotIn("123:abc", str(ctx.exception))
        self.assertTrue(ctx.exception.retryable)


class OutboxTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.telegram = self.create_channel("telegram", "Bot", "bot-1", credential="123:abc")
        self.send_webhook(self.telegram, telegram_message(text="ping"))
        self.conv_id = self.api("GET", "/api/conversations").get_json()["items"][0]["id"]

    def reply(self, body="pong"):
        resp = self.api("POST", f"/api/conversations/{self.conv_id}/messages", json={"body": body})
        self.assertEqual(resp.status_code, 201)
        return resp.get_json()

    def deliver(self):
        return self.api("POST", "/api/outbox/deliver").get_json()

    def message(self, message_id):
        items = self.api("GET", f"/api/conversations/{self.conv_id}/messages").get_json()["items"]
        return next(m for m in items if m["id"] == message_id)

    def test_successful_delivery(self):
        msg = self.reply()
        with mock.patch(POST_JSON, return_value=(200, {"ok": True, "result": {"message_id": 9}})):
            self.assertEqual(self.deliver(), {"sent": 1, "retried": 0, "failed": 0})
        delivered = self.message(msg["id"])
        self.assertEqual(delivered["delivery_status"], "sent")
        self.assertIsNone(delivered["delivery_error"])
        # Nothing left to do on the next pass.
        with mock.patch(POST_JSON) as post:
            self.assertEqual(self.deliver(), {"sent": 0, "retried": 0, "failed": 0})
        post.assert_not_called()

    def test_permanent_failure_records_error(self):
        msg = self.reply()
        with mock.patch(POST_JSON, return_value=(403, {"ok": False, "description": "Forbidden: bot was blocked"})):
            self.assertEqual(self.deliver(), {"sent": 0, "retried": 0, "failed": 1})
        failed = self.message(msg["id"])
        self.assertEqual(failed["delivery_status"], "failed")
        self.assertIn("blocked", failed["delivery_error"])

        listed = self.api("GET", "/api/outbox?status=failed").get_json()
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["message_id"], msg["id"])
        self.assertEqual(listed["items"][0]["attempts"], 1)

    def test_transient_failure_backs_off_then_gives_up(self):
        self.app.config["OUTBOX_MAX_ATTEMPTS"] = 2
        msg = self.reply()
        with mock.patch(POST_JSON, return_value=(503, {})):
            self.assertEqual(self.deliver(), {"sent": 0, "retried": 1, "failed": 0})
            # Not due yet: backoff is in the future.
            self.assertEqual(self.deliver(), {"sent": 0, "retried": 0, "failed": 0})
        entry = self.api("GET", "/api/outbox").get_json()["items"][0]
        self.assertEqual(entry["status"], "pending")
        self.assertIsNotNone(entry["next_attempt_at"])
        self.assertIn("503", entry["last_error"])

        # Make it due now and fail again: second attempt is the last one.
        with self.app.app_context():
            from aiochat.db import get_db
            get_db().execute("UPDATE outbox SET next_attempt_at = '2000-01-01T00:00:00.000Z'")
            get_db().commit()
        with mock.patch(POST_JSON, return_value=(503, {})):
            self.assertEqual(self.deliver(), {"sent": 0, "retried": 0, "failed": 1})
        self.assertEqual(self.message(msg["id"])["delivery_status"], "failed")

    def test_retry_failed_message(self):
        msg = self.reply()
        with mock.patch(POST_JSON, return_value=(403, {"ok": False, "description": "blocked"})):
            self.deliver()
        resp = self.api("POST", f"/api/outbox/{msg['id']}/retry")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "pending")
        self.assertEqual(resp.get_json()["attempts"], 0)
        # Only failed entries can be retried.
        self.assertEqual(self.api("POST", f"/api/outbox/{msg['id']}/retry").status_code, 409)
        self.assertEqual(self.api("POST", "/api/outbox/999/retry").status_code, 404)
        with mock.patch(POST_JSON, return_value=(200, {"ok": True, "result": {"message_id": 9}})):
            self.assertEqual(self.deliver(), {"sent": 1, "retried": 0, "failed": 0})

    def test_channel_without_credential_waits(self):
        slack = self.create_channel("slack", "Acme", "T1")
        self.send_webhook(slack, slack_message())
        conv = self.api("GET", f"/api/conversations?channel_id={slack['id']}").get_json()["items"][0]
        msg = self.api("POST", f"/api/conversations/{conv['id']}/messages", json={"body": "hey"}).get_json()
        with mock.patch(POST_JSON) as post:
            self.assertEqual(self.deliver(), {"sent": 0, "retried": 0, "failed": 0})
        post.assert_not_called()
        entry = self.api("GET", "/api/outbox?status=pending").get_json()["items"][0]
        self.assertEqual(entry["message_id"], msg["id"])
        self.assertFalse(entry["can_send"])

        # Setting the credential makes it eligible.
        resp = self.api("PATCH", f"/api/channels/{slack['id']}", json={"credential": "xoxb-1"})
        self.assertTrue(resp.get_json()["can_send"])
        with mock.patch(POST_JSON, return_value=(200, {"ok": True, "ts": "1761500000.000900"})) as post:
            self.assertEqual(self.deliver(), {"sent": 1, "retried": 0, "failed": 0})
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer xoxb-1"})

    def test_slack_echo_of_own_message_is_deduplicated(self):
        slack = self.create_channel("slack", "Acme", "T1", credential="xoxb-1")
        self.send_webhook(slack, slack_message(channel="C1"))
        conv = self.api("GET", f"/api/conversations?channel_id={slack['id']}").get_json()["items"][0]
        self.api("POST", f"/api/conversations/{conv['id']}/messages", json={"body": "hey"})
        with mock.patch(POST_JSON, return_value=(200, {"ok": True, "ts": "1761500000.000900"})):
            self.deliver()
        # Slack now sends our own message back as an event with the same ts.
        echo = self.send_webhook(slack, slack_message(channel="C1", user="UBOT", text="hey", ts="1761500000.000900"))
        self.assertEqual(echo.get_json(), counts(duplicates=1))
        total = self.api("GET", f"/api/conversations/{conv['id']}/messages").get_json()["total"]
        self.assertEqual(total, 2)

    def test_slack_echo_arriving_first_is_replaced(self):
        slack = self.create_channel("slack", "Acme", "T1", credential="xoxb-1")
        self.send_webhook(slack, slack_message(channel="C1"))
        conv = self.api("GET", f"/api/conversations?channel_id={slack['id']}").get_json()["items"][0]
        msg = self.api("POST", f"/api/conversations/{conv['id']}/messages", json={"body": "hey"}).get_json()

        def post_then_echo(*args, **kwargs):
            self.send_webhook(slack, slack_message(channel="C1", user="UBOT", text="hey", ts="1761500000.000900"))
            return 200, {"ok": True, "ts": "1761500000.000900"}

        with mock.patch(POST_JSON, side_effect=post_then_echo):
            self.assertEqual(self.deliver(), {"sent": 1, "retried": 0, "failed": 0})
        items = self.api("GET", f"/api/conversations/{conv['id']}/messages").get_json()["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual([m["direction"] for m in items], ["outbound", "inbound"])
        self.assertEqual(items[0]["id"], msg["id"])
        self.assertEqual(items[0]["delivery_status"], "sent")

    def test_unexpected_connector_error_does_not_stop_the_pass(self):
        self.reply("one")
        self.reply("two")
        calls = []

        def flaky(url, payload, **kwargs):
            calls.append(payload["text"])
            if payload["text"] == "one":
                raise RuntimeError("boom")
            return 200, {"ok": True, "result": {"message_id": 5}}

        with mock.patch(POST_JSON, side_effect=flaky), self.assertLogs("aiochat.services.outbox", level="ERROR"):
            self.assertEqual(self.deliver(), {"sent": 1, "retried": 1, "failed": 0})
        self.assertEqual(calls, ["one", "two"])

    def test_claim_is_atomic(self):
        """A row claimed by another worker is skipped, not sent twice."""
        msg = self.reply()
        with self.app.app_context():
            from aiochat.db import get_db
            rows = get_db().execute("SELECT id, attempts FROM outbox").fetchall()
            # Simulate a concurrent worker claiming the row after our SELECT.
            self.assertFalse(outbox_service._claim(get_db(), rows[0]["id"], rows[0]["attempts"] + 1, "now"))
            self.assertTrue(outbox_service._claim(get_db(), rows[0]["id"], rows[0]["attempts"], "now"))
            self.assertFalse(outbox_service._claim(get_db(), rows[0]["id"], rows[0]["attempts"], "now"))
        self.assertEqual(self.message(msg["id"])["delivery_status"], "pending")

    def test_invalid_status_filter(self):
        self.assertEqual(self.api("GET", "/api/outbox?status=lost").status_code, 400)


class WorkerTests(AppTestCase):
    def test_worker_delivers_when_woken(self):
        telegram = self.create_channel("telegram", "Bot", "bot-1", credential="123:abc")
        self.send_webhook(telegram, telegram_message())
        conv_id = self.api("GET", "/api/conversations").get_json()["items"][0]["id"]

        # Observe the worker's passes directly rather than polling the API on
        # a timer, so the test is not sensitive to machine speed.
        delivered = threading.Event()
        real_deliver = outbox_service.deliver_pending

        def deliver_and_signal():
            result = real_deliver()
            if result["sent"]:
                delivered.set()
            return result

        worker = OutboxWorker(self.app, interval=60)
        with mock.patch("aiochat.worker.deliver_pending", side_effect=deliver_and_signal), \
                mock.patch(POST_JSON, return_value=(200, {"ok": True, "result": {"message_id": 1}})) as post:
            worker.start()
            self.app.extensions["outbox_worker"] = worker
            msg = self.api("POST", f"/api/conversations/{conv_id}/messages", json={"body": "hi"}).get_json()
            self.assertTrue(delivered.wait(timeout=30), "worker never delivered the reply")
            worker.stop()

        items = self.api("GET", f"/api/conversations/{conv_id}/messages").get_json()["items"]
        self.assertEqual(next(m["delivery_status"] for m in items if m["id"] == msg["id"]), "sent")
        self.assertEqual(post.call_count, 1)
        self.assertFalse(worker.is_alive())

    def test_worker_survives_pass_errors(self):
        worker = OutboxWorker(self.app, interval=60)
        with mock.patch("aiochat.worker.deliver_pending", side_effect=RuntimeError("db gone")):
            with self.assertLogs(self.app.logger, level="ERROR"):
                self.assertEqual(worker.run_once(), {})


if __name__ == "__main__":
    unittest.main()
