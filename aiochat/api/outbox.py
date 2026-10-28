"""/api/outbox — delivery status of replies, and manual control."""

from flask import Blueprint, jsonify, request

from ..auth import require_api_token
from ..services import outbox as service
from .params import pagination

bp = Blueprint("outbox", __name__, url_prefix="/api/outbox")
bp.before_request(require_api_token)


@bp.get("")
def list_entries():
    page, page_size = pagination()
    return jsonify(service.list_entries(page, page_size, status=request.args.get("status")))


@bp.post("/deliver")
def deliver_now():
    """Run one delivery pass synchronously (useful without a worker)."""
    return jsonify(service.deliver_pending())


@bp.post("/<int:message_id>/retry")
def retry(message_id: int):
    return jsonify(service.retry(message_id))
