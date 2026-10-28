"""Minimal JSON-over-HTTP client for platform APIs (standard library only).

Kept in one place so tests can replace ``post_json`` and so connectors never
touch ``urllib`` directly.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .base import DeliveryError


def post_json(
    url: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> tuple[int, dict]:
    """POST ``payload`` as JSON and return ``(status, decoded_body)``.

    HTTP error statuses are returned, not raised, because platform APIs put
    useful detail in the body. Network failures and non-JSON responses raise
    a retryable ``DeliveryError``.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise DeliveryError(f"network error: {reason}", retryable=True) from exc

    try:
        body = json.loads(raw or b"{}")
    except ValueError as exc:
        raise DeliveryError(f"non-JSON response (HTTP {status})", retryable=True) from exc
    if not isinstance(body, dict):
        raise DeliveryError(f"unexpected response (HTTP {status})", retryable=True)
    return status, body
