"""Small Telegram message-ID journal. Never stores message text or business data."""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)
WINDOW = 48 * 3600 - 60  # Leave time for Telegram to receive the delete request.


def value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def topic_id(message, fields=None):
    fields = fields or {}
    topic = value(message, "message_thread_id") or fields.get("message_thread_id")
    if topic:
        return int(topic)
    chat = value(message, "chat", {})
    return 1 if value(chat, "is_forum", False) else 0


def is_clear(text):
    """Exact command only; /clear_month, ordinary text and captions are unrelated."""
    parts = str(text or "").strip().split(maxsplit=1)
    return bool(parts and parts[0].split("@", 1)[0].casefold() == "/clear")


def error_details(error):
    details = getattr(error, "result_json", None) or {}
    code = getattr(error, "error_code", None) or getattr(error, "code", 0) or details.get("error_code", 0)
    retry = getattr(error, "retry_after", 0) or details.get("parameters", {}).get("retry_after", 0)
    return int(code or 0), int(retry or 0)


def open_history(path, token, chats):
    try:
        return MessageHistory(path, token, chats)
    except (sqlite3.Error, OSError, ValueError):
        LOGGER.warning("Telegram message history is unavailable; normal bot work continues")
        return None


class MessageHistory:
    def __init__(self, path, token, chats):
        self.path = Path(path)
        self.scope = hashlib.sha256(token.encode()).hexdigest()[:24]
        self.chats = {int(chat) for chat in chats if chat}
        self.lock = threading.RLock()
        self.next_cleanup = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path, timeout=2, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS telegram_messages (
                bot TEXT NOT NULL, chat INTEGER NOT NULL, topic INTEGER NOT NULL,
                message INTEGER NOT NULL, sent REAL NOT NULL, not_before REAL NOT NULL,
                controls INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(bot,chat,message)
            );
            CREATE INDEX IF NOT EXISTS telegram_message_scope
                ON telegram_messages(bot,chat,topic,message);
        """)

    def close(self):
        with self.lock:
            self.db.close()

    def remember(self, message, fields=None):
        """Logging failure must never turn a successful send into a duplicate retry."""
        try:
            self._remember(message, fields or {})
        except (sqlite3.Error, OSError, ValueError, TypeError):
            LOGGER.warning("Telegram message history could not be updated")

    def _remember(self, message, fields):
        chat = value(message, "chat", {})
        chat_id = int(value(chat, "id", 0) or 0)
        ident, sent = value(message, "message_id", 0), value(message, "date", 0)
        now = time.time()
        if (chat_id not in self.chats or not isinstance(ident, int) or ident <= 0 or
                not isinstance(sent, (int, float)) or not now - WINDOW <= sent <= now + 60):
            return
        if any(value(message, key) for key in ("forum_topic_created", "supergroup_chat_created", "channel_chat_created", "group_chat_created")):
            return  # Telegram does not permit deleting topic/chat creation messages.
        markup = value(message, "reply_markup")
        if markup is None:
            markup = fields.get("reply_markup")
        if isinstance(markup, str):
            try:
                markup = json.loads(markup)
            except ValueError:
                markup = None
        controls = bool(value(markup, "inline_keyboard"))
        not_before = sent + 86400 if value(chat, "type") == "private" and value(message, "dice") else 0
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO telegram_messages VALUES(?,?,?,?,?,?,?) ON CONFLICT(bot,chat,message) "
                "DO UPDATE SET sent=MIN(sent,excluded.sent),controls=excluded.controls,not_before=MAX(not_before,excluded.not_before) "
                "WHERE topic=excluded.topic",
                (self.scope, chat_id, topic_id(message, fields), ident, sent, not_before, int(controls)),
            )
            if now >= self.next_cleanup:
                self.db.execute("DELETE FROM telegram_messages WHERE sent<?", (now - 48 * 3600,))
                self.next_cleanup = now + 3600

    def observe_update(self, update):
        message = value(update, "message") or value(update, "edited_message")
        query = value(update, "callback_query")
        self.remember(message or value(query, "message"))

    def forget(self, chat, identifiers):
        with self.lock, self.db:
            self.db.executemany("DELETE FROM telegram_messages WHERE bot=? AND chat=? AND message=?",
                                ((self.scope, chat, ident) for ident in identifiers))

    def clear(self, message, delete):
        """Only known IDs in this topic, through this command. Never guess ID ranges.

        Working cards with inline controls stay usable. New concurrent messages and
        pending business work are outside this snapshot. Failed deletes can be retried.
        """
        chat = int(value(value(message, "chat", {}), "id", 0))
        topic, through, now = topic_id(message), int(value(message, "message_id", 0)), time.time()
        if chat not in self.chats or through <= 0:
            return "Не можу очистити цей чат."
        with self.lock:
            rows = self.db.execute(
                "SELECT message,controls,not_before FROM telegram_messages WHERE bot=? AND chat=? AND topic=? "
                "AND message<=? AND sent>=? ORDER BY message DESC",
                (self.scope, chat, topic, through, now - WINDOW),
            ).fetchall()
        identifiers = [row[0] for row in rows if not row[1] and row[2] <= now]
        preserved = any(row[1] for row in rows)
        pending = [identifiers[n:n + 100] for n in range(0, len(identifiers), 100)]
        failed, requests, code, retry = False, 0, 0, 0
        while pending and requests < 32:
            batch = pending.pop(0)
            requests += 1
            try:
                if delete(chat, batch) is not True:
                    failed = True
                    break
                self.forget(chat, batch)
            except Exception as error:
                code, retry = error_details(error)
                # One undeletable message must not block the whole batch. Bound
                # requests so a large or damaged history cannot flood Telegram.
                if code == 400 and len(batch) > 1:
                    midpoint = len(batch) // 2
                    pending[0:0] = [batch[:midpoint], batch[midpoint:]]
                    continue
                failed = True
                if code != 400:
                    break
        place = "цій темі" if topic else "цьому чаті"
        if code == 429:
            text = "Telegram просить трохи зачекати. Повтори /clear" + (" через %s с." % retry if retry else " трохи пізніше.")
        elif code == 403:
            text = "Для очищення групи надай боту право адміністратора «Видалення повідомлень», потім повтори /clear."
        elif failed or pending:
            text = "Прибрав доступну частину повідомлень у %s. Решту можна спробувати очистити ще раз командою /clear." % place
        else:
            text = "Готово — доступні повідомлення в %s прибрано." % place
        if preserved:
            text += " Картки з робочими кнопками залишив."
        return text + "\nСтаріше 48 годин і повідомлення, яких бот не бачив, можуть залишитися."
