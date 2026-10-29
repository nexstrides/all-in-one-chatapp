"""/api/contacts: the accounts seen on each channel, for grouping into people."""

from flask import Blueprint, jsonify

from ..auth import require_api_token
from ..services import people as service
from .params import flag, optional_int, optional_query, pagination

bp = Blueprint("contacts", __name__, url_prefix="/api/contacts")
bp.before_request(require_api_token)


@bp.get("")
def list_contacts():
    page, page_size = pagination()
    return jsonify(
        service.list_contacts(
            page,
            page_size,
            channel_id=optional_int("channel_id"),
            person_id=optional_int("person_id"),
            unlinked=flag("unlinked"),
            query=optional_query("q"),
        )
    )


@bp.get("/<int:contact_id>")
def get_contact(contact_id: int):
    return jsonify(service.get_contact(contact_id))
