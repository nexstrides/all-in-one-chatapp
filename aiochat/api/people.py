"""/api/people — group a person's accounts across platforms by hand."""

from flask import Blueprint, jsonify

from ..auth import require_api_token
from ..services import inbox
from ..services import people as service
from .params import flag, id_list, json_body, pagination, required_str

bp = Blueprint("people", __name__, url_prefix="/api/people")
bp.before_request(require_api_token)

MAX_NAME_LENGTH = 100


@bp.get("")
def list_people():
    page, page_size = pagination()
    return jsonify(service.list_people(page, page_size))


@bp.post("")
def create_person():
    data = json_body()
    name = required_str(data, "name", max_length=MAX_NAME_LENGTH)
    contact_ids = id_list(data, "contact_ids") if data.get("contact_ids") is not None else None
    return jsonify(service.create_person(name, contact_ids)), 201


@bp.get("/<int:person_id>")
def get_person(person_id: int):
    return jsonify(service.get_person(person_id))


@bp.patch("/<int:person_id>")
def update_person(person_id: int):
    data = json_body()
    return jsonify(service.update_person(person_id, required_str(data, "name", max_length=MAX_NAME_LENGTH)))


@bp.delete("/<int:person_id>")
def delete_person(person_id: int):
    service.delete_person(person_id)
    return "", 204


@bp.post("/<int:person_id>/contacts")
def link_contacts(person_id: int):
    """``{"contact_ids": [...]}``. Contacts already on another person are refused (409)."""
    return jsonify(service.link_contacts(person_id, id_list(json_body(), "contact_ids")))


@bp.delete("/<int:person_id>/contacts/<int:contact_id>")
def unlink_contact(person_id: int, contact_id: int):
    return jsonify(service.unlink_contact(person_id, contact_id))


@bp.get("/<int:person_id>/timeline")
def timeline(person_id: int):
    page, page_size = pagination()
    return jsonify(
        inbox.list_person_messages(person_id, page, page_size, include_deleted=flag("include_deleted"))
    )
