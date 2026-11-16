"""Application entry point."""

from __future__ import annotations

import logging
import signal
import threading
from typing import Any

import telebot

from backup import start_backup_worker
from business_db import init_business_db
from callbacks import handle_callback
from config import (
    BACKUP_WORKER_ENABLED,
    BOT_TOKEN,
    LOG_LEVEL,
    TG_ALLOWED_PRIVATE_USER_IDS,
    TG_OWNER_USER_ID,
    validate_config,
)
from db_runtime import close_pools
from handlers import handle_private_message
from incoming_group_listener import handle_incoming_group_message
from tracker_worker import start_tracker
from tracking_db import cleanup_processed_messages, init_db
from telegram_clear import install_clear

LOGGER = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def register_handlers(bot: telebot.TeleBot) -> None:
    install_clear(bot)

    @bot.callback_query_handler(func=lambda call: True)
    def callback_queries(call: Any) -> None:
        try:
            handle_callback(bot, call)
        except Exception:
            LOGGER.exception("Telegram button action failed")
            try:
                bot.answer_callback_query(
                    call.id,
                    "Не вдалося виконати дію. Спробуй ще раз.",
                    show_alert=True,
                )
            except Exception:
                LOGGER.debug("Could not show callback error", exc_info=True)

    @bot.message_handler(
        func=lambda message: message.chat.type == "private",
        content_types=["text", "photo", "document"],
    )
    def private_messages(message: Any) -> None:
        try:
            handle_private_message(bot, message)
        except Exception:
            LOGGER.exception("Private Telegram message failed")
            try:
                bot.send_message(
                    message.chat.id,
                    "Не вдалося виконати дію. Дані не втрачено — спробуй ще раз.",
                )
            except Exception:
                LOGGER.debug("Could not show private-message error", exc_info=True)

    @bot.message_handler(
        func=lambda message: message.chat.type in ("group", "supergroup"),
        content_types=["text", "photo", "document"],
    )
    def group_messages(message: Any) -> None:
        handle_incoming_group_message(bot, message)


def register_commands(bot: telebot.TeleBot) -> None:
    try:
        basic_commands = [
            telebot.types.BotCommand("status", "Перевірити ТТН"),
            telebot.types.BotCommand("whoami", "Мій Telegram ID"),
            telebot.types.BotCommand("help", "Допомога"),
        ]
        bot.set_my_commands(basic_commands)
        if TG_OWNER_USER_ID:
            owner_commands = [
                telebot.types.BotCommand("add_product", "Додати товар"),
                telebot.types.BotCommand("stats", "Статистика"),
                telebot.types.BotCommand("stock", "Склад і товари"),
                telebot.types.BotCommand("turnover", "Оборот"),
                telebot.types.BotCommand("status", "Перевірити ТТН"),
                telebot.types.BotCommand("unlock", "Відкрити фінанси паролем"),
                telebot.types.BotCommand("lock", "Закрити фінанси"),
                telebot.types.BotCommand("clear_month", "Очистити поточний місяць"),
                telebot.types.BotCommand("health", "Стан бота"),
                telebot.types.BotCommand("retry", "Повторити відстеження"),
                telebot.types.BotCommand("export", "Експорт CSV"),
                telebot.types.BotCommand("export1c", "Експорт для 1С"),
                telebot.types.BotCommand("backup", "Резервна копія"),
                telebot.types.BotCommand("whoami", "Мій Telegram ID"),
                telebot.types.BotCommand("help", "Допомога"),
            ]
            bot.set_my_commands(
                owner_commands,
                scope=telebot.types.BotCommandScopeChat(chat_id=TG_OWNER_USER_ID),
            )
    except Exception:
        LOGGER.warning("Could not register Telegram commands", exc_info=True)


def run() -> None:
    configure_logging()
    validate_config()
    if not TG_OWNER_USER_ID and not TG_ALLOWED_PRIVATE_USER_IDS:
        LOGGER.warning(
            "TG_OWNER_USER_ID and TG_ALLOWED_PRIVATE_USER_IDS are empty; private "
            "submissions are disabled until an owner is configured"
        )
    init_db()
    init_business_db()
    removed = cleanup_processed_messages()
    if removed:
        LOGGER.info("Removed %s old idempotency records", removed)

    bot: telebot.TeleBot | None = None
    worker = None
    repliq_worker = None
    backup_worker = None
    stop_event = threading.Event()
    backup_stop_event = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()
        backup_stop_event.set()
        if bot is not None:
            bot.stop_polling()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=4)
        register_handlers(bot)
        register_commands(bot)
        worker, stop_event = start_tracker(bot)
        from repliq_inbox import start_receiver

        repliq_worker = start_receiver(bot, stop_event)
        if BACKUP_WORKER_ENABLED:
            backup_worker, backup_stop_event = start_backup_worker()

        LOGGER.info("Bot polling started")
        bot.infinity_polling(
            timeout=20,
            long_polling_timeout=20,
            skip_pending=False,
            allowed_updates=["message", "callback_query"],
        )
    finally:
        stop_event.set()
        backup_stop_event.set()
        if bot is not None:
            bot.stop_polling()
        if worker is not None:
            worker.join(timeout=10)
        if repliq_worker is not None:
            repliq_worker.join(timeout=5)
        if backup_worker is not None:
            backup_worker.join(timeout=10)
        close_pools()
        history = getattr(bot, "_message_history", None) if bot is not None else None
        if history:
            history.close()
        LOGGER.info("Bot stopped")


if __name__ == "__main__":
    run()
