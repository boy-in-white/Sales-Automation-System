"""Owner-only /clear. Deletes Telegram history, never orders or accounting."""

import functools
import logging
import sqlite3
import time

from config import BOT_TOKEN, DATABASE_PATH, TG_GROUP_ID, TG_OWNER_USER_ID, is_owner
from telegram_history import is_clear, open_history, value

LOGGER = logging.getLogger(__name__)


def install_clear(bot):
    """Observe this bot instance; preserve existing handlers and send results."""
    if getattr(bot, "_clear_installed", False):
        return bot._message_history
    history = open_history(DATABASE_PATH.parent / "telegram-messages.sqlite3", BOT_TOKEN,
                           [TG_GROUP_ID, TG_OWNER_USER_ID])
    bot._clear_installed, bot._message_history = True, history

    def wrap_send(original):
        @functools.wraps(original)
        def send(*args, **kwargs):
            result = original(*args, **kwargs)
            if history:
                for message in result if isinstance(result, list) else [result]:
                    history.remember(message, kwargs)
            return result
        return send

    for name in ("send_message", "send_document", "send_photo", "send_media_group",
                 "send_audio", "send_video", "send_voice", "send_sticker", "send_animation",
                 "forward_message", "copy_message", "edit_message_text", "edit_message_caption",
                 "edit_message_reply_markup"):
        method = getattr(bot, name, None)
        if callable(method):
            setattr(bot, name, wrap_send(method))

    original_process = bot.process_new_updates

    @functools.wraps(original_process)
    def process(updates):
        if history:
            for update in updates:
                history.observe_update(update)
        return original_process(updates)

    bot.process_new_updates = process

    # Register before general text handlers. No keyboard or menu button is added.
    @bot.message_handler(func=lambda message: is_clear(value(message, "text")), content_types=["text"])
    def clear_command(message):
        handle_clear(bot, history, message)

    return history


def handle_clear(bot, history, message):
    chat, sender = value(message, "chat", {}), value(message, "from_user", {})
    owner = value(sender, "id", 0)
    if (not is_owner(owner) or value(sender, "is_bot") or
            value(chat, "id") not in (TG_OWNER_USER_ID, TG_GROUP_ID) or
            value(chat, "type") not in ("private", "group", "supergroup") or
            any(value(message, key) for key in ("sender_chat", "forward_origin", "forward_date", "via_bot")) or
            not 0 <= time.time() - value(message, "date", 0) <= 900):
        return
    text = str(value(message, "text", "") or "").strip()
    fields = {"chat_id": value(chat, "id")}
    if value(message, "message_thread_id"):
        fields["message_thread_id"] = value(message, "message_thread_id")
    try:
        command = text.split(maxsplit=1)[0]
        me = None
        if "@" in command:
            me = bot.get_me()
            if command.split("@", 1)[1].casefold() != str(value(me, "username", "")).casefold():
                return
        if len(text.split(maxsplit=1)) > 1:
            notice = "Щоб очистити повідомлення тут, напиши просто /clear."
        elif not history:
            notice = "Очищення тимчасово недоступне. Звичайна робота бота продовжується."
        else:
            if value(chat, "id") == TG_GROUP_ID:
                me = me or bot.get_me()
                member = bot.get_chat_member(TG_GROUP_ID, value(me, "id"))
                if value(member, "status") != "creator" and not (value(member, "status") == "administrator" and value(member, "can_delete_messages")):
                    bot.send_message(text="Надай Ledgerly право адміністратора «Видалення повідомлень», потім повтори /clear.", **fields)
                    return
            history.remember(message)
            notice = history.clear(message, bot.delete_messages)
        bot.send_message(text=notice, **fields)
    except sqlite3.Error:
        bot.send_message(text="Не вдалося завершити очищення. Спробуй /clear ще раз. Замовлення збережені.", **fields)
    except Exception:
        # Do not dump Telegram request URLs (tokens) or a customer's message.
        LOGGER.warning("Telegram /clear could not finish; normal bot work continues")
