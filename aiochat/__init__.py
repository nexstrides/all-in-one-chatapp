"""All-in-One Chat: one inbox for conversations from several chat platforms."""

from flask import Flask, jsonify

from . import db
from .api import register_blueprints
from .config import Config
from .errors import register_error_handlers
from .worker import deliver_outbox_command, purge_deleted_command


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    register_error_handlers(app)
    register_blueprints(app)
    app.cli.add_command(deliver_outbox_command)
    app.cli.add_command(purge_deleted_command)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    with app.app_context():
        db.init_db()

    return app
