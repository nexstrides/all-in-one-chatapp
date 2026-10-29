"""Request parsing helpers shared by the API blueprints."""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from flask import current_app, request

from ..connectors.base import iso_utc
from ..errors import ApiError


def _positive_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_parameter", f"'{name}' must be an integer.") from None
    if value < 1:
        raise ApiError(400, "invalid_parameter", f"'{name}' must be at least 1.")
    return value


def pagination() -> tuple[int, int]:
    page = _positive_int("page", request.args.get("page", "1"))
    default_size = current_app.config["DEFAULT_PAGE_SIZE"]
    page_size = _positive_int("page_size", request.args.get("page_size", str(default_size)))
    return page, min(page_size, current_app.config["MAX_PAGE_SIZE"])


def optional_int(name: str) -> int | None:
    raw = request.args.get(name)
    return None if raw is None else _positive_int(name, raw)


def flag(name: str) -> bool:
    return request.args.get(name, "").lower() in {"1", "true", "yes"}


def json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "Request body must be a JSON object.")
    return data


def required_str(data: dict, key: str, max_length: int | None = None) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "invalid_field", f"'{key}' is required.")
    value = value.strip()
    if max_length is not None and len(value) > max_length:
        raise ApiError(400, "invalid_field", f"'{key}' must be at most {max_length} characters.")
    return value


def optional_str(data: dict, key: str, max_length: int | None = None) -> str | None:
    """Like ``required_str`` but the key may be absent or null."""
    if data.get(key) is None:
        return None
    return required_str(data, key, max_length=max_length)


def required_query(name: str, max_length: int = 200) -> str:
    value = request.args.get(name, "").strip()
    if not value:
        raise ApiError(400, "invalid_parameter", f"'{name}' query parameter is required.")
    if len(value) > max_length:
        raise ApiError(400, "invalid_parameter", f"'{name}' must be at most {max_length} characters.")
    return value


def optional_query(name: str, max_length: int = 200) -> str | None:
    """A free-text query parameter that may be absent or blank."""
    value = request.args.get(name, "").strip()
    if not value:
        return None
    if len(value) > max_length:
        raise ApiError(400, "invalid_parameter", f"'{name}' must be at most {max_length} characters.")
    return value


def optional_time_bound(name: str, end: bool = False) -> str | None:
    """Parse a date or timestamp parameter into the stored ISO format.

    Accepts ``YYYY-MM-DD`` or an ISO 8601 timestamp (``Z`` or an offset;
    naive means UTC). A bare date is the start of that UTC day, or its last
    millisecond when ``end`` is set, so ``since=D&until=D`` covers one day.
    """
    raw = request.args.get(name)
    if raw is None or not raw.strip():
        return None
    raw = raw.strip()
    try:
        if len(raw) == 10:
            day = date.fromisoformat(raw)
            edge = time(23, 59, 59, 999000) if end else time.min
            dt = datetime.combine(day, edge, tzinfo=timezone.utc)
        else:
            if raw[-1] in "Zz":  # fromisoformat only accepts 'Z' from 3.11
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ApiError(
            400, "invalid_parameter", f"'{name}' must be a date (YYYY-MM-DD) or an ISO 8601 timestamp."
        ) from None
    return iso_utc(dt)


def id_list(data: dict, key: str) -> list[int]:
    """A non-empty JSON array of positive integers, de-duplicated in order."""
    raw = data.get(key)
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "invalid_field", f"'{key}' must be a non-empty array of ids.")
    ids: list[int] = []
    for value in raw:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ApiError(400, "invalid_field", f"'{key}' must contain positive integers.")
        if value not in ids:
            ids.append(value)
    return ids
