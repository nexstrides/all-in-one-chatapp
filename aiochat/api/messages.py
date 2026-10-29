"""/api/messages — search across every conversation."""

from flask import Blueprint, jsonify, request

from ..auth import require_api_token
from ..services import inbox
from ..textmatch import DEFAULT_MODE
from .params import optional_int, optional_time_bound, pagination, required_query

bp = Blueprint("messages", __name__, url_prefix="/api/messages")
bp.before_request(require_api_token)


@bp.get("")
def search():
    page, page_size = pagination()
    return jsonify(
        inbox.search_messages(
            required_query("q"),
            page,
            page_size,
            match=request.args.get("match", DEFAULT_MODE).strip().lower(),
            person_id=optional_int("person_id"),
            channel_id=optional_int("channel_id"),
            platform=request.args.get("platform", "").strip().lower() or None,
            since=optional_time_bound("since"),
            until=optional_time_bound("until", end=True),
        )
    )
