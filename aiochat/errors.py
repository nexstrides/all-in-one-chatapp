"""API error type and JSON error handlers."""

from flask import jsonify
from werkzeug.exceptions import HTTPException


class ApiError(Exception):
    """Raise from a route or service to return a JSON error response."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _error_body(code: str, message: str):
    return {"error": {"code": code, "message": message}}


def register_error_handlers(app) -> None:
    @app.errorhandler(ApiError)
    def handle_api_error(err: ApiError):
        return jsonify(_error_body(err.code, err.message)), err.status

    @app.errorhandler(HTTPException)
    def handle_http_error(err: HTTPException):
        code = (err.name or "error").lower().replace(" ", "_")
        return jsonify(_error_body(code, err.description or err.name)), err.code
