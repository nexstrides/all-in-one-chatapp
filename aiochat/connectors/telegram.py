"""Telegram Bot API connector.

Handles ``message`` updates with text. Photos, stickers, edited messages and
other update types are ignored for now.
"""

from __future__ import annotations

from .base import Connector, InboundMessage, PayloadError, iso_from_epoch, require


def _full_name(user: dict) -> str:
    parts = [user.get("first_name"), user.get("last_name")]
    name = " ".join(p for p in parts if p)
    return name or user.get("username") or str(user.get("id"))


class TelegramConnector(Connector):
    platform = "telegram"

    def parse(self, payload: dict) -> list[InboundMessage]:
        message = payload.get("message")
        if not message or "text" not in message:
            return []

        sender = require(message, "from", "message")
        chat = require(message, "chat", "message")
        message_id = require(message, "message_id", "message")
        date = require(message, "date", "message")
        if not isinstance(date, (int, float)):
            raise PayloadError(f"invalid date '{date}'")

        if chat.get("type") == "private":
            title = _full_name(chat)
        else:
            title = chat.get("title") or str(chat.get("id"))

        return [
            InboundMessage(
                external_conversation_id=str(require(chat, "id", "chat")),
                conversation_title=title,
                external_user_id=str(require(sender, "id", "from")),
                user_display_name=_full_name(sender),
                external_message_id=str(message_id),
                body=message["text"],
                sent_at=iso_from_epoch(date),
            )
        ]
