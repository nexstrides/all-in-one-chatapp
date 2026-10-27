"""/api/conversations — the unified inbox seen by the client."""

from flask import Blueprint, jsonify, request

from ..auth import require_api_token
from ..errors import ApiError
from ..services import inbox
from .params import flag, json_body, optional_int, pagination, required_str

bp = Blueprint("conversations", __name__, url_prefix="/api/conversations")
bp.before_request(require_api_token)

MAX_REPLY_LENGTH = 4000


@bp.get("")
def list_conversations():
    page, page_size = pagination()
    return jsonify(
        inbox.list_conversations(
            page,
            page_size,
            channel_id=optional_int("channel_id"),
            unread_only=flag("unread"),
        )
    )


@bp.get("/<int:conversation_id>")
def get_conversation(conversation_id: int):
    return jsonify(inbox.get_conversation(conversation_id))


@bp.get("/<int:conversation_id>/messages")
def list_messages(conversation_id: int):
    page, page_size = pagination()
    return jsonify(inbox.list_messages(conversation_id, page, page_size))


@bp.post("/<int:conversation_id>/messages")
def send_reply(conversation_id: int):
    data = json_body()
    body = required_str(data, "body", max_length=MAX_REPLY_LENGTH)
    return jsonify(inbox.reply(conversation_id, body)), 201


@bp.post("/<int:conversation_id>/read")
def mark_read(conversation_id: int):
    # The body is optional: an empty POST marks the whole conversation read.
    data = json_body() if request.get_data() else {}
    up_to = data.get("up_to_message_id")
    if up_to is not None and (not isinstance(up_to, int) or isinstance(up_to, bool) or up_to < 1):
        raise ApiError(400, "invalid_field", "'up_to_message_id' must be a positive integer.")
    updated = inbox.mark_read(conversation_id, up_to)
    return jsonify({"updated": updated})
