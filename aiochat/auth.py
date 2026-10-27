"""Bearer-token auth for the client-facing API.

Inbound webhooks are not covered here: each channel authenticates its own
webhook calls with a per-channel token (see api/webhooks.py).
"""

import hmac

from flask import current_app, request

from .errors import ApiError


def require_api_token() -> None:
    """``before_request`` hook for blueprints that need the API token."""
    expected = current_app.config.get("API_TOKEN")
    if not expected:
        return  # auth disabled (local development)

    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, expected):
        raise ApiError(401, "unauthorized", "Missing or invalid API token.")
