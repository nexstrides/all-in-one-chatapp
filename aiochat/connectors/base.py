"""Connector interface.

A connector turns one platform's webhook payload into a list of normalized
inbound events (new messages, edits, deletions), and sends replies back
through the platform's API. Connectors never touch the database; the only network access is in
``send`` (through ``connectors.http``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Union


@dataclass(frozen=True)
class InboundMessage:
    external_conversation_id: str
    conversation_title: str
    external_user_id: str
    user_display_name: str
    external_message_id: str
    body: str
    sent_at: str  # ISO 8601 UTC, e.g. 2026-10-26T21:00:00.000Z


@dataclass(frozen=True)
class InboundEdit:
    """The text of an existing message changed."""

    external_conversation_id: str
    external_message_id: str
    body: str
    edited_at: str


@dataclass(frozen=True)
class InboundDelete:
    """An existing message was removed on the platform."""

    external_conversation_id: str
    external_message_id: str
    deleted_at: str


InboundEvent = Union[InboundMessage, InboundEdit, InboundDelete]


class PayloadError(ValueError):
    """The payload is malformed for this platform."""


class DeliveryError(Exception):
    """Sending a message failed.

    ``retryable`` is True for transient problems (network, rate limits, 5xx)
    and False for things that will not fix themselves (bad token, unknown
    chat). Messages must never contain credentials.
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class Connector:
    platform: str = ""

    def handshake(self, payload: dict) -> dict | None:
        """Return a response body if the payload is a verification handshake.

        Some platforms send a one-off challenge when a webhook URL is first
        registered. Return ``None`` for ordinary event payloads.
        """
        return None

    def parse(self, payload: dict) -> list[InboundEvent]:
        """Return the events in ``payload``; an empty list means 'ignore'."""
        raise NotImplementedError

    def send(
        self, credential: str, external_conversation_id: str, body: str, timeout: float = 10
    ) -> str:
        """Deliver ``body`` to a conversation and return the platform's message id.

        ``credential`` is the channel's API token. Raise ``DeliveryError`` on
        failure.
        """
        raise NotImplementedError


def iso_from_epoch(seconds: float) -> str:
    """Format a Unix timestamp the same way SQLite's defaults do."""
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def require(mapping: dict, key: str, where: str):
    if not isinstance(mapping, dict) or key not in mapping:
        raise PayloadError(f"missing '{key}' in {where}")
    return mapping[key]
