"""Load demo data by replaying sample webhooks through the real API.

    python scripts/seed.py

Uses the database from AIOCHAT_DATABASE (default instance/aiochat.sqlite3).
Running it twice is safe: channels are reused and messages are deduplicated.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiochat import create_app  # noqa: E402
from aiochat.db import get_db  # noqa: E402

BASE_TS = 1_761_500_000  # 2025-10-26, an arbitrary fixed point


def slack_event(channel, channel_name, user, name, text, offset):
    return {
        "type": "event_callback",
        "team_id": "T-DEMO",
        "event": {
            "type": "message",
            "channel": channel,
            "channel_name": channel_name,
            "user": user,
            "user_profile": {"display_name": name},
            "text": text,
            "ts": f"{BASE_TS + offset}.000100",
        },
    }


def telegram_update(update_id, chat_id, user_id, first, last, text, offset):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user_id, "first_name": first, "last_name": last},
            "chat": {"id": chat_id, "type": "private", "first_name": first, "last_name": last},
            "date": BASE_TS + offset,
            "text": text,
        },
    }


SLACK = [
    slack_event("C-GENERAL", "general", "U-MIA", "Mia", "Morning! Standup in 10?", 0),
    slack_event("C-GENERAL", "general", "U-OMAR", "Omar", "Running 5 late, start without me", 60),
    slack_event("C-GENERAL", "general", "U-OMAR", "Omar", "I'll miss standup tomorrow, flying out early", 90),
    slack_event("C-SUPPORT", "support", "U-LENA", "Lena", "Customer says exports are empty", 300),
]

# Mia is on both platforms, under a different display name on each.
TELEGRAM = [
    telegram_update(1, 9001, 9001, "Sam", "Rivera", "Hey, are we still on for Friday?", 120),
    telegram_update(2, 9001, 9001, "Sam", "Rivera", "I can bring the projector", 180),
    telegram_update(3, 9002, 9002, "Mia", "Park", "Can you send me the standup notes?", 240),
]


def ensure_channel(client, platform, name, account_id):
    row = get_db().execute(
        "SELECT id, webhook_token FROM channels WHERE platform = ? AND external_account_id = ?",
        (platform, account_id),
    ).fetchone()
    if row:
        return row["id"], row["webhook_token"]
    resp = client.post(
        "/api/channels",
        json={"platform": platform, "name": name, "external_account_id": account_id},
        headers=auth_headers(),
    )
    if resp.status_code != 201:
        raise SystemExit(f"could not create {platform} channel: {resp.get_json()}")
    data = resp.get_json()
    return data["id"], data["webhook_token"]


def auth_headers():
    token = os.environ.get("AIOCHAT_API_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def main():
    app = create_app()
    client = app.test_client()
    with app.app_context():
        for platform, name, account, payloads in (
            ("slack", "Acme Slack", "T-DEMO", SLACK),
            ("telegram", "Support bot", "bot-demo", TELEGRAM),
        ):
            channel_id, token = ensure_channel(client, platform, name, account)
            for payload in payloads:
                resp = client.post(
                    f"/api/webhooks/{platform}/{channel_id}",
                    json=payload,
                    headers={"X-Webhook-Token": token},
                )
                if resp.status_code != 200:
                    raise SystemExit(f"{platform} webhook failed: {resp.get_json()}")
            print(f"{platform}: channel {channel_id}, {len(payloads)} events sent")


if __name__ == "__main__":
    main()
