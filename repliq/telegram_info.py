"""User-run setup helper. Prints IDs only; never prints messages or bot tokens."""

import os
import re

from app import ROOT, AppError, private_lock, read_json
from telegram_bridge import Telegram, TelegramError


def main():
    os.umask(0o077)
    data_dir = ROOT / "private"
    try:
        with private_lock(data_dir / "responder-state.lock", blocking=False):
            token = read_json(data_dir / "telegram.json").get("bot_token")
            if not isinstance(token, str) or not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,100}", token):
                raise AppError("Спочатку впиши токен окремого Repliq-бота в private/telegram.json.")
            api = Telegram(token)
            me = api.call("getMe", {})
            if api.call("getWebhookInfo", {}).get("url"):
                raise AppError("Цей бот уже має webhook. Використай окремого Telegram-бота для Repliq.")
            print("Бот: @%s (ID %s)" % (me.get("username", ""), me["id"]))
            updates = api.call("getUpdates", {"offset": 0, "limit": 100, "timeout": 0, "allowed_updates": ["message"]})
            seen = set()
            for update in updates:
                message = update.get("message", {})
                sender, chat = message.get("from", {}), message.get("chat", {})
                pair = (sender.get("id"), chat.get("id"))
                if pair in seen or sender.get("is_bot") or not pair[0] or not pair[1]:
                    continue
                seen.add(pair)
                print("owner_id: %s · chat_id: %s · тип: %s" % (pair[0], pair[1], chat.get("type", "")))
            if not seen:
                print("Напиши /start боту особисто та /id у групі. Потім повтори цю команду.")
    except BlockingIOError:
        print("Спочатку зупини Repliq: перевірка ID не має працювати одночасно з основним ботом.")
        return 1
    except (AppError, TelegramError) as error:
        print(str(error))
        return 1
    except (OSError, ValueError):
        print("Не вдалося прочитати налаштування Telegram. Перевір private/telegram.json.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
