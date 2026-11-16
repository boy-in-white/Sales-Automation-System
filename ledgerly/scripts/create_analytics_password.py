#!/usr/bin/env python3
"""Create a password hash for owner-only financial Telegram commands."""

from __future__ import annotations

import getpass
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from auth import create_password_hash  # noqa: E402


def main() -> None:
    first = getpass.getpass("Придумай пароль для фінансів: ")
    second = getpass.getpass("Повтори пароль: ")
    if first != second:
        raise SystemExit("Паролі не збігаються. Запусти команду ще раз.")
    try:
        encoded = create_password_hash(first)
    except ValueError:
        raise SystemExit("Пароль має містити щонайменше 8 символів.") from None
    print("\nДодай цей рядок у .env:")
    print(f"ANALYTICS_PASSWORD_HASH={encoded}")
    print("Сам пароль збережи окремо і не надсилай у чат.")


if __name__ == "__main__":
    main()
