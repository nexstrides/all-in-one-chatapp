"""Slack connector.

Inbound: Events API ``url_verification`` handshakes and ``message`` events:

* plain messages (no ``subtype``) become ``InboundMessage``;
* ``message_changed`` becomes ``InboundEdit`` (or ``InboundDelete`` when the
  new message is a ``tombstone``, which is how Slack reports deleting a
  thread parent that still has replies);
* ``message_deleted`` becomes ``InboundDelete``;
* every other subtype (joins, legacy bot messages, ...) is ignored for now.

Outbound: ``chat.postMessage`` with a bot token (``xoxb-...``) that has the
``chat:write`` scope. Slack echoes the bot's own message back as a
``message`` event; it carries the same ``ts`` we store on the outbound
message, so the echo is deduplicated on ingest.
"""

from __future__ import annotations

from . import http
from .base import (
    Connector,
    DeliveryError,
    InboundDelete,
    InboundEdit,
    InboundEvent,
    InboundMessage,
    PayloadError,
    iso_from_epoch,
    require,
)

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

# Slack API errors worth retrying; everything else (invalid_auth,
# channel_not_found, not_in_channel, ...) needs a human.
_TRANSIENT_ERRORS = {"ratelimited", "internal_error", "service_unavailable", "fatal_error"}


def _ts_to_iso(ts, what: str) -> str:
    try:
        return iso_from_epoch(float(ts))
    except (TypeError, ValueError) as exc:
        raise PayloadError(f"invalid {what} '{ts}'") from exc


class SlackConnector(Connector):
    platform = "slack"

    def handshake(self, payload: dict) -> dict | None:
        if payload.get("type") == "url_verification":
            return {"challenge": require(payload, "challenge", "url_verification")}
        return None

    def parse(self, payload: dict) -> list[InboundEvent]:
        if payload.get("type") != "event_callback":
            return []

        event = require(payload, "event", "event_callback")
        if event.get("type") != "message":
            return []

        subtype = event.get("subtype")
        if not subtype:
            return [self._new_message(event)]
        if subtype == "message_changed":
            return [self._changed(event)]
        if subtype == "message_deleted":
            return [self._deleted(event)]
        return []

    def _new_message(self, event: dict) -> InboundMessage:
        channel = require(event, "channel", "message event")
        user = require(event, "user", "message event")
        ts = require(event, "ts", "message event")
        sent_at = _ts_to_iso(ts, "ts")

        profile = event.get("user_profile") or {}
        display_name = profile.get("display_name") or profile.get("real_name") or user

        # Events do not include channel names. Direct messages are titled
        # after the person; channels fall back to the id until renamed.
        if event.get("channel_type") == "im":
            title = display_name
        else:
            title = event.get("channel_name") or channel

        return InboundMessage(
            external_conversation_id=channel,
            conversation_title=title,
            external_user_id=user,
            user_display_name=display_name,
            # Slack's ts is unique per channel and doubles as the message id.
            external_message_id=str(ts),
            body=event.get("text", ""),
            sent_at=sent_at,
        )

    def _changed(self, event: dict) -> InboundEvent:
        channel = require(event, "channel", "message_changed event")
        message = require(event, "message", "message_changed event")
        ts = require(message, "ts", "message_changed message")
        # The event's own ts is when the edit happened; message.edited.ts
        # says the same thing when present.
        edited_at = _ts_to_iso((message.get("edited") or {}).get("ts") or event.get("ts"), "ts")

        if message.get("subtype") == "tombstone":
            return InboundDelete(
                external_conversation_id=channel,
                external_message_id=str(ts),
                deleted_at=edited_at,
            )
        return InboundEdit(
            external_conversation_id=channel,
            external_message_id=str(ts),
            body=message.get("text", ""),
            edited_at=edited_at,
        )

    def _deleted(self, event: dict) -> InboundDelete:
        return InboundDelete(
            external_conversation_id=require(event, "channel", "message_deleted event"),
            external_message_id=str(require(event, "deleted_ts", "message_deleted event")),
            deleted_at=_ts_to_iso(event.get("ts") or event.get("event_ts"), "ts"),
        )

    def send(
        self, credential: str, external_conversation_id: str, body: str, timeout: float = 10
    ) -> str:
        status, data = http.post_json(
            POST_MESSAGE_URL,
            {"channel": external_conversation_id, "text": body},
            headers={"Authorization": f"Bearer {credential}"},
            timeout=timeout,
        )
        if status == 429 or status >= 500:
            raise DeliveryError(f"slack: HTTP {status}", retryable=True)
        if not data.get("ok"):
            error = data.get("error") or f"HTTP {status}"
            raise DeliveryError(f"slack: {error}", retryable=error in _TRANSIENT_ERRORS)
        ts = data.get("ts")
        if not ts:
            raise DeliveryError("slack: response has no ts", retryable=False)
        return str(ts)
