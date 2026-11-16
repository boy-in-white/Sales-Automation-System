#!/usr/bin/env python3
"""Show Telegram user, group and topic IDs without starting the bot."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import requests


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config import BOT_TOKEN  # noqa: E402


def _messages(update: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    result: list[tuple[str, dict[str, Any]]] = []
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        value = update.get(key)
        if isinstance(value, dict):
            result.append((key, value))
    callback = update.get("callback_query")
    if isinstance(callback, dict) and isinstance(callback.get("message"), dict):
        result.append(("callback_query", callback["message"]))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show Telegram IDs from the bot's latest updates",
    )
    parser.add_argument("--limit", type=int, default=100, help="number of updates (1-100)")
    arguments = parser.parse_args()

    if not BOT_TOKEN or "replace-me" in BOT_TOKEN.casefold():
        raise SystemExit("Add BOT_TOKEN to .env first.")

    try:
        response = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"limit": max(1, min(arguments.limit, 100)), "timeout": 0},
            timeout=20,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise SystemExit(
            f"Could not read Telegram updates ({type(exc).__name__}). "
            "Check the internet connection and BOT_TOKEN."
        ) from None

    if not response.ok or not payload.get("ok"):
        description = str(payload.get("description") or "Telegram API error")
        raise SystemExit(description[:300])

    found = 0
    seen: set[tuple[int, int, int]] = set()
    for update in payload.get("result", []):
        for kind, message in _messages(update):
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            chat_id = int(chat.get("id") or 0)
            user_id = int(sender.get("id") or 0)
            topic_id = int(message.get("message_thread_id") or 0)
            key = (chat_id, topic_id, int(message.get("message_id") or 0))
            if key in seen:
                continue
            seen.add(key)
            text = str(message.get("text") or message.get("caption") or "")
            text = " ".join(text.split())[:80]
            print(
                f"{kind}: user_id={user_id} chat_id={chat_id} "
                f"topic_id={topic_id} text={text!r}"
            )
            found += 1

    if not found:
        print("No updates found.")
        print("Stop the bot, send it /whoami, and send one message in every topic.")
        print("Then run this command again.")


if __name__ == "__main__":
    main()
