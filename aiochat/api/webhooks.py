"""/api/webhooks — inbound events from external platforms.

Each channel gets its own URL, ``/api/webhooks/<platform>/<channel_id>``, and
must send the channel's token in the ``X-Webhook-Token`` header. These routes
do not use the client API token.
"""

import hmac

from flask import Blueprint, jsonify, request

from ..connectors import PayloadError, get_connector
from ..errors import ApiError
from ..services import inbox
from ..services.channels import get_channel_row
from .params import json_body

bp = Blueprint("webhooks", __name__, url_prefix="/api/webhooks")


@bp.post("/<platform>/<int:channel_id>")
def receive(platform: str, channel_id: int):
    connector = get_connector(platform)
    if connector is None:
        raise ApiError(404, "not_found", f"Unknown platform '{platform}'.")

    channel = get_channel_row(channel_id)
    if channel["platform"] != platform:
        raise ApiError(404, "not_found", "Channel not found for this platform.")

    token = request.headers.get("X-Webhook-Token", "")
    if not hmac.compare_digest(token, channel["webhook_token"]):
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
