"""/api/messages — search across every conversation."""

from flask import Blueprint, jsonify

from ..auth import require_api_token
from ..services import inbox
from .params import pagination, required_query

bp = Blueprint("messages", __name__, url_prefix="/api/messages")
bp.before_request(require_api_token)


@bp.get("")
def search():
    page, page_size = pagination()
    return jsonify(inbox.search_messages(required_query("q"), page, page_size))
