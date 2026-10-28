from . import channels, conversations, messages, outbox, webhooks


def register_blueprints(app) -> None:
    app.register_blueprint(channels.bp)
    app.register_blueprint(conversations.bp)
    app.register_blueprint(messages.bp)
    app.register_blueprint(outbox.bp)
    app.register_blueprint(webhooks.bp)
