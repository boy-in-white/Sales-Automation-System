"""Small, non-spammy administrator alerts."""

from __future__ import annotations

import logging
from typing import Any

from config import TG_ALERTS_THREAD_ID, TG_GROUP_ID


LOGGER = logging.getLogger(__name__)


def notify_admin(bot: Any, text: str) -> bool:
    try:
        bot.send_message(
            TG_GROUP_ID,
            f"⚠️ Системне повідомлення\n\n{text}"[:4000],
            message_thread_id=TG_ALERTS_THREAD_ID,
        )
        return True
    except Exception:
        LOGGER.exception("Could not send administrator alert")
        return False

