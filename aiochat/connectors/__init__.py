"""Registry of supported platforms.

To add a platform, implement ``Connector`` in a new module and register an
instance here.
"""

from .base import (
    Connector,
    DeliveryError,
    InboundDelete,
    InboundEdit,
    InboundEvent,
    InboundMessage,
    PayloadError,
)
from .slack import SlackConnector
from .telegram import TelegramConnector

CONNECTORS: dict[str, Connector] = {
    c.platform: c for c in (SlackConnector(), TelegramConnector())
}


def get_connector(platform: str) -> Connector | None:
    return CONNECTORS.get(platform)


__all__ = [
    "CONNECTORS",
    "Connector",
    "DeliveryError",
    "InboundDelete",
    "InboundEdit",
    "InboundEvent",
    "InboundMessage",
    "PayloadError",
    "get_connector",
]
