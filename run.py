"""Development server: python run.py"""

import os

from aiochat import create_app

app = create_app()

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", "5000")), debug=True)
