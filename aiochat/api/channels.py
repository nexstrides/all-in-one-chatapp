"""/api/channels — connect and disconnect external accounts."""

from flask import Blueprint, jsonify

from ..auth import require_api_token
from ..services import channels as service
from .params import json_body, optional_str, required_str

bp = Blueprint("channels", __name__, url_prefix="/api/channels")
bp.before_request(require_api_token)


@bp.get("")
def list_channels():
    return jsonify({"items": service.list_channels()})


@bp.post("")
def create_channel():
    data = json_body()
    channel = service.create_channel(
        platform=required_str(data, "platform"),
        name=required_str(data, "name", max_length=100),
        external_account_id=required_str(data, "external_account_id"),
        credential=optional_str(data, "credential"),
    )
    return jsonify(channel), 201


@bp.get("/<int:channel_id>")
def get_channel(channel_id: int):
    return jsonify(service.get_channel(channel_id))


@bp.patch("/<int:channel_id>")
def update_channel(channel_id: int):
    """Rename a channel or set/clear its credential (``"credential": null``)."""
    data = json_body()
    kwargs = {}
    if "name" in data:
        kwargs["name"] = required_str(data, "name", max_length=100)
    if "credential" in data:
        kwargs["credential"] = optional_str(data, "credential")
    return jsonify(service.update_channel(channel_id, **kwargs))


@bp.delete("/<int:channel_id>")
def delete_channel(channel_id: int):
    service.delete_channel(channel_id)
    return "", 204
