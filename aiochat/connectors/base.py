"""Connector interface.

A connector turns one platform's webhook payload into a list of normalized
``InboundMessage`` objects. Connectors are pure: no database, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class InboundMessage:
    external_conversation_id: str
    conversation_title: str
    external_user_id: str
    user_display_name: str
    external_message_id: str
    body: str
    sent_at: str  # ISO 8601 UTC, e.g. 2026-10-26T21:00:00.000Z


class PayloadError(ValueError):
    """The payload is malformed for this platform."""


class Connector:
    platform: str = ""

    def handshake(self, payload: dict) -> dict | None:
        """Return a response body if the payload is a verification handshake.

        Some platforms send a one-off challenge when a webhook URL is first
        registered. Return ``None`` for ordinary event payloads.
        """
        return None

    def parse(self, payload: dict) -> list[InboundMessage]:
        """Return the messages in ``payload``; an empty list means 'ignore'."""
        raise NotImplementedError


def iso_from_epoch(seconds: float) -> str:
    """Format a Unix timestamp the same way SQLite's defaults do."""
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def require(mapping: dict, key: str, where: str):
    if not isinstance(mapping, dict) or key not in mapping:
        raise PayloadError(f"missing '{key}' in {where}")
    return mapping[key]
