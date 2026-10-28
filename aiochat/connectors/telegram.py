"""Telegram Bot API connector.

Inbound: ``message`` updates with text become ``InboundMessage``;
``edited_message`` updates with text become ``InboundEdit``. Photos,
stickers, caption edits and other update types are ignored for now.

Telegram does not tell bots when a message is deleted, so deletions never
arrive from this platform.

Outbound: ``sendMessage`` with the bot token. Telegram never echoes a bot's
own messages back through the webhook, so no dedup concerns here.
"""

from __future__ import annotations

from . import http
from .base import (
    Connector,
    DeliveryError,
    InboundEdit,
    InboundEvent,
    InboundMessage,
    PayloadError,
    iso_from_epoch,
    require,
)

API_BASE = "https://api.telegram.org"


def _full_name(user: dict) -> str:
    parts = [user.get("first_name"), user.get("last_name")]
    name = " ".join(p for p in parts if p)
    return name or user.get("username") or str(user.get("id"))


def _epoch(value, what: str) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PayloadError(f"invalid {what} '{value}'")
    return iso_from_epoch(value)


class TelegramConnector(Connector):
    platform = "telegram"

    def parse(self, payload: dict) -> list[InboundEvent]:
        message = payload.get("message")
        if message and "text" in message:
            return [self._new_message(message)]
        edited = payload.get("edited_message")
        if edited and "text" in edited:
            return [self._edited(edited)]
        return []

    def _new_message(self, message: dict) -> InboundMessage:
        sender = require(message, "from", "message")
        chat = require(message, "chat", "message")
        message_id = require(message, "message_id", "message")
        sent_at = _epoch(require(message, "date", "message"), "date")

        if chat.get("type") == "private":
            title = _full_name(chat)
        else:
            title = chat.get("title") or str(chat.get("id"))

        return InboundMessage(
            external_conversation_id=str(require(chat, "id", "chat")),
            conversation_title=title,
            external_user_id=str(require(sender, "id", "from")),
            user_display_name=_full_name(sender),
            external_message_id=str(message_id),
            body=message["text"],
            sent_at=sent_at,
        )

    def _edited(self, message: dict) -> InboundEdit:
        chat = require(message, "chat", "edited_message")
        message_id = require(message, "message_id", "edited_message")
        edited_at = _epoch(require(message, "edit_date", "edited_message"), "edit_date")
        return InboundEdit(
            external_conversation_id=str(require(chat, "id", "chat")),
            external_message_id=str(message_id),
            body=message["text"],
            edited_at=edited_at,
        )

    def send(
        self, credential: str, external_conversation_id: str, body: str, timeout: float = 10
    ) -> str:
        # The bot token is part of the URL, so scrub it from anything we
        # might store or log.
        try:
            status, data = http.post_json(
                f"{API_BASE}/bot{credential}/sendMessage",
                {"chat_id": external_conversation_id, "text": body},
                timeout=timeout,
            )
        except DeliveryError as exc:
            raise DeliveryError(
                str(exc).replace(credential, "***"), retryable=exc.retryable
            ) from None

        if status == 429 or status >= 500:
            raise DeliveryError(f"telegram: HTTP {status}", retryable=True)
        if not data.get("ok"):
            description = data.get("description") or f"HTTP {status}"
            raise DeliveryError(f"telegram: {description}", retryable=False)
        message_id = (data.get("result") or {}).get("message_id")
        if message_id is None:
            raise DeliveryError("telegram: response has no message_id", retryable=False)
        return str(message_id)
