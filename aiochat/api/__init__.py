from . import channels, conversations, webhooks


def register_blueprints(app) -> None:
    app.register_blueprint(channels.bp)
    app.register_blueprint(conversations.bp)
    app.register_blueprint(webhooks.bp)
