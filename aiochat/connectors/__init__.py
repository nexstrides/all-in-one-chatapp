"""Registry of supported platforms.

To add a platform, implement ``Connector`` in a new module and register an
instance here.
"""

from .base import Connector, InboundMessage, PayloadError
from .slack import SlackConnector
from .telegram import TelegramConnector

CONNECTORS: dict[str, Connector] = {
    c.platform: c for c in (SlackConnector(), TelegramConnector())
}


def get_connector(platform: str) -> Connector | None:
    return CONNECTORS.get(platform)


__all__ = ["CONNECTORS", "Connector", "InboundMessage", "PayloadError", "get_connector"]
