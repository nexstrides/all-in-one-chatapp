"""/api/webhooks — inbound events from external platforms.

Each channel gets its own URL, ``/api/webhooks/<platform>/<channel_id>``, and
every call must carry the channel's webhook token. Platforms differ in how
they can present it, so three places are accepted:

* ``?token=...`` in the URL — Slack's Events API only lets you configure a
  URL, so the token rides along in the query string.
* ``X-Telegram-Bot-Api-Secret-Token`` header — what Telegram sends when the
  webhook was registered with ``secret_token``.
* ``X-Webhook-Token`` header — for anything else (tests, curl).

These routes do not use the client API token.
"""

import hmac

from flask import Blueprint, jsonify, request

from ..connectors import PayloadError, get_connector
from ..errors import ApiError
from ..services import inbox
from ..services.channels import get_channel_row
from .params import json_body

bp = Blueprint("webhooks", __name__, url_prefix="/api/webhooks")

_TOKEN_HEADERS = ("X-Webhook-Token", "X-Telegram-Bot-Api-Secret-Token")


def _presented_token() -> str:
    for header in _TOKEN_HEADERS:
        value = request.headers.get(header)
        if value:
            return value
    return request.args.get("token", "")


@bp.post("/<platform>/<int:channel_id>")
def receive(platform: str, channel_id: int):
    connector = get_connector(platform)
    if connector is None:
        raise ApiError(404, "not_found", f"Unknown platform '{platform}'.")

    channel = get_channel_row(channel_id)
    if channel["platform"] != platform:
        raise ApiError(404, "not_found", "Channel not found for this platform.")

    if not hmac.compare_digest(_presented_token(), channel["webhook_token"]):
        raise ApiError(401, "unauthorized", "Invalid webhook token.")

    payload = json_body()

    handshake = connector.handshake(payload)
    if handshake is not None:
        return jsonify(handshake)

    try:
        messages = connector.parse(payload)
    except PayloadError as exc:
        raise ApiError(400, "invalid_payload", str(exc)) from None

    result = inbox.ingest(channel_id, messages)
    return jsonify(result)
