"""Slack Events API connector.

Handles ``url_verification`` handshakes and plain ``message`` events. Edited,
deleted and bot messages carry a ``subtype`` and are ignored for now.
"""

from __future__ import annotations

from .base import Connector, InboundMessage, PayloadError, iso_from_epoch, require


class SlackConnector(Connector):
    platform = "slack"

    def handshake(self, payload: dict) -> dict | None:
        if payload.get("type") == "url_verification":
            return {"challenge": require(payload, "challenge", "url_verification")}
        return None

    def parse(self, payload: dict) -> list[InboundMessage]:
        if payload.get("type") != "event_callback":
            return []

        event = require(payload, "event", "event_callback")
        if event.get("type") != "message" or event.get("subtype"):
            return []

        channel = require(event, "channel", "message event")
        user = require(event, "user", "message event")
        ts = require(event, "ts", "message event")
        try:
            sent_at = iso_from_epoch(float(ts))
        except (TypeError, ValueError) as exc:
            raise PayloadError(f"invalid ts '{ts}'") from exc

        profile = event.get("user_profile") or {}
        display_name = profile.get("display_name") or profile.get("real_name") or user

        return [
            InboundMessage(
                external_conversation_id=channel,
                conversation_title=event.get("channel_name") or channel,
                external_user_id=user,
                user_display_name=display_name,
                # Slack's ts is unique per channel and doubles as the message id.
                external_message_id=str(ts),
                body=event.get("text", ""),
                sent_at=sent_at,
            )
        ]
