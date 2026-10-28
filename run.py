"""Development server: python run.py"""

import os

from aiochat import create_app
from aiochat.worker import start_outbox_worker

app = create_app()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "1") != "0"
    # With the debug reloader, the parent process only watches files; the
    # child (WERKZEUG_RUN_MAIN=true) serves requests and should own the worker.
    serving_process = not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if app.config["OUTBOX_WORKER"] and serving_process:
        start_outbox_worker(app)
    app.run(port=int(os.environ.get("PORT", "5000")), debug=debug)
