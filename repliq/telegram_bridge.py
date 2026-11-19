"""Telegram forum inbox. Only explicitly authorized owners can relay text to OLX."""

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app import AppError, read_json
from catalog_admin import admin_owner
from handoff_store import quote_entities, status_text
from product_registry import Registry
from order_board import OrderBoard
from telegram_errors import TelegramError, error_reason
from telegram_history import is_clear, open_history
from telegram_controls import TopicControls


def settings(data_dir):
    data = read_json(Path(data_dir) / "telegram.json")
    if not data.get("enabled", False):
        return None
    if (data.get("enabled") is not True or not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,100}", str(data.get("bot_token", ""))) or
            type(data.get("group_id")) is not int or data["group_id"] >= 0 or
            not isinstance(data.get("owner_ids"), list) or not data["owner_ids"] or
            any(type(i) is not int or i <= 0 for i in data["owner_ids"])):
        raise AppError("Перевір private/telegram.json: окремий bot_token, group_id групи й owner_ids власників.")
    if type(admin_owner(data)) is not int or admin_owner(data) not in data["owner_ids"]:
        raise AppError("Для приватного керування вкажи одного owner_id або admin_owner_id зі списку owner_ids.")
    return data


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class Telegram:
    def __init__(self, token, history=None):
        self.base = "https://api.telegram.org/bot" + token + "/"
        self.opener = build_opener(NoRedirect())
        self.history = history

    def call(self, method, fields, document=None):
        if document is None:
            body = json.dumps(fields).encode("utf-8")
            content_type = "application/json"
        else:
            boundary = "repliq" + uuid.uuid4().hex
            parts = []
            for name, value in fields.items():
                parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, name, value)).encode())
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"document\"; filename=\"olx-history.txt\"\r\n"
                          "Content-Type: text/plain; charset=utf-8\r\n\r\n" % boundary).encode())
            parts.extend((document.encode("utf-8"), ("\r\n--%s--\r\n" % boundary).encode()))
            body, content_type = b"".join(parts), "multipart/form-data; boundary=" + boundary
        request = Request(self.base + method, data=body, headers={"Content-Type": content_type})
        try:
            timeout = max(20, min(40, int(fields.get('timeout', 0)) + 15)) if method == 'getUpdates' else 12
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise TelegramError(method=method)
                result = json.loads(raw)
        except HTTPError as error:
            retry_after, reason = 0, ""
            try:
                details = json.loads(error.read(65536))
                retry_after = int(details.get("parameters", {}).get("retry_after", 0))
                reason = error_reason(details.get("description", ""))
            except (ValueError, TypeError, AttributeError):
                pass
            raise TelegramError(error.code, retry_after, reason, method) from None
        except (URLError, OSError, ValueError, UnicodeError):
            raise TelegramError(method=method) from None
        if not isinstance(result, dict) or not result.get("ok"):
            raise TelegramError(result.get("error_code", 0) if isinstance(result, dict) else 0,
                                reason=error_reason(result.get("description", "")) if isinstance(result, dict) else "", method=method)
        result = result["result"]
        if self.history:
            for message in result if isinstance(result, list) else [result]:
                self.history.remember(message, fields)
        return result


class Bridge(threading.Thread):
    def __init__(self, handoffs, config, account, stop, output=print, api=None):
        super().__init__(name="repliq-telegram", daemon=True)
        self.handoffs, self.store, self.config = handoffs, handoffs.store, config
        self.account, self.stop, self.output = account, stop, output
        self.history = open_history(Path(self.store.path).parent / "telegram-messages.sqlite3",
                                    config["bot_token"], [config["group_id"], admin_owner(config)])
        self.api = api or Telegram(config["bot_token"], self.history)
        self.topic_controls = TopicControls(self.store, account, config['group_id'], self.api)
        self.scope = hashlib.sha256(config["bot_token"].encode()).hexdigest()[:20]
        self.offset_key = "telegram-offset:" + self.scope
        self.next_send = 0.0
        self.next_private_send = 0.0
        self.admin_worker = None
        self.last_private_activity = 0.0
        self.registry = Registry(self.store, account)
        self.orders = OrderBoard(self.store)
        self.next_delivery, self.health_at, self.health_value = 0.0, 0.0, None
        self.last_error, self.last_error_at = None, 0.0
        self.update_failures = {}
        self.polling_interrupted = False
        self.next_title_sync = 0.0

    def health(self, status, code=0, reason="", phase="", retry_at=0):
        value = (status, code, reason, phase)
        if value != self.health_value or time.monotonic() - self.health_at >= 60:
            try:
                self.store.set_meta("telegram-status:" + self.account, {"status": status, "code": code,
                                    "reason": reason, "phase": phase, "updated": time.time(), "retry_at": retry_at})
            except sqlite3.Error:
                return  # A diagnostic heartbeat must not break message processing.
            self.health_at, self.health_value = time.monotonic(), value

    def report_error(self, error, phase):
        transient_poll = phase == 'getUpdates' and (not error.code or error.code >= 500)
        if transient_poll:
            self.polling_interrupted = True
        signature = (error.code, error.reason, phase)
        if signature != self.last_error or time.monotonic() - self.last_error_at >= 60:
            self.output("Telegram: тимчасово втрачено зв’язок. Перепідключаюся автоматично; OLX працює." if transient_poll else
                        str(error) + " Відповіді OLX у режимі бота продовжуються; ручний режим зберігається.")
            try:
                self.store.set_meta("telegram-last-error:" + self.account, {"code": error.code, "reason": error.reason,
                                    "phase": phase, "method": error.method, "updated": time.time()})
            except sqlite3.Error:
                pass
            self.last_error, self.last_error_at = signature, time.monotonic()

    def repair_topic(self, item, error):
        """Only a definite rejection permits retrying a send. Never guess on timeout."""
        if error.reason == "topic_closed":
            try:
                self.api.call("reopenForumTopic", {"chat_id": item["group_id"], "message_thread_id": item["topic_id"]})
            except TelegramError as reopened:
                if reopened.reason != "not_modified":
                    raise
        elif error.reason == "topic_missing":
            with self.store.db() as db:
                db.execute("UPDATE handoffs SET topic_id=NULL,topic_state='new',control_message_id=NULL,updated=? "
                           "WHERE account=? AND thread=? AND topic_id=?", (time.time(), self.account, item["thread"], item["topic_id"]))
                # Text already queued for an old topic must not reach a newly assigned one.
                db.execute("UPDATE bridge_replies SET status='cancelled',note='Тему Telegram змінено; повтори відповідь у новій темі' "
                           "WHERE account=? AND thread=? AND status='queued'", (self.account, item["thread"]))
                # A repaired topic must not replay every ordinary customer message.
                self.handoffs.enqueue(db, "topic-context:%s:%s:%s" % (self.account, item["thread"], item["topic_id"]),
                                      self.account, item["thread"], "notice", "Тему відновлено. Керування: auto / я.")
            self.handoffs.control(self.account, item["thread"], status_text(item["mode"]),
                                  "topic-repair:%s:%s:%s" % (self.account, item["thread"], item["topic_id"]))
        else:
            return False
        with self.store.db() as db:
            db.execute("UPDATE bridge_outbox SET status='pending' WHERE id=?", (item["id"],))
        self.next_send = time.monotonic() + 3.2
        self.output("Telegram: відновлюю тему клієнта. Обраний режим керування збережено.")
        return True

    @staticmethod
    def controls(mode, card=None, expanded=False):
        def button(text, action):
            return {"text": text, "callback_data": "chat:" + action}
        rows = [[button("✓ auto" if mode in ("bot", "waiting", "returning") else "auto", "bot"),
                 button("✓ я" if mode == "human" else "я", "take")]]
        return {"inline_keyboard": rows}

    def sync_topic_name(self):
        if time.monotonic() < max(self.next_send, self.next_title_sync):
            return
        with self.store.db() as db:
            row = db.execute("SELECT h.* FROM handoffs h JOIN effective_ads a ON a.account=h.account AND a.advert=h.advert "
                             "WHERE h.account=? AND h.group_id=? AND h.topic_id IS NOT NULL AND h.topic_state='ready' "
                             "AND h.telegram_title!=h.title LIMIT 1", (self.account, self.config['group_id'])).fetchone()
        if not row:
            self.next_title_sync = time.monotonic() + 10
            return
        try:
            self.api.call('editForumTopic', {'chat_id': row['group_id'], 'message_thread_id': row['topic_id'], 'name': row['title'][:100]})
        except TelegramError as error:
            if error.reason != 'not_modified':
                self.next_title_sync = time.monotonic() + max(60, error.retry_after)
                if error.code == 429:
                    self.next_send = self.next_private_send = self.next_title_sync
                return
        with self.store.db() as db:
            db.execute('UPDATE handoffs SET telegram_title=? WHERE account=? AND thread=? AND topic_id=?',
                       (row['title'], self.account, row['thread'], row['topic_id']))
        self.next_send = time.monotonic() + 3.2

    def bootstrap(self):
        self.me = self.api.call("getMe", {})
        webhook = self.api.call("getWebhookInfo", {})
        if webhook.get("url"):
            raise AppError("У Telegram-бота встановлено webhook. Для Repliq потрібен окремий бот без webhook.")
        chat = self.api.call("getChat", {"chat_id": self.config["group_id"]})
        if chat.get("type") != "supergroup" or not chat.get("is_forum") or chat.get("username"):
            raise AppError("Для Repliq потрібна приватна Telegram-супергрупа з увімкненими темами.")
        member = self.api.call("getChatMember", {"chat_id": self.config["group_id"], "user_id": self.me["id"]})
        if member.get("status") != "administrator" or not member.get("can_manage_topics"):
            raise AppError("Надай Repliq права адміністратора Telegram-групи з керуванням темами.")
        offset = self.store.meta(self.offset_key)
        if "value" not in offset:
            # First connection must not send old Telegram commands to customers.
            latest = self.api.call("getUpdates", {"offset": -1, "limit": 1, "timeout": 0, "allowed_updates": ["message", "callback_query"]})
            value = max((u["update_id"] for u in latest), default=-1) + 1
            self.store.set_meta(self.offset_key, {"value": value})

    def create_topic(self):
        if time.monotonic() < self.next_send:
            return False
        with self.store.db() as db:
            row = db.execute("SELECT h.* FROM handoffs h JOIN effective_ads a ON a.account=h.account AND a.advert=h.advert "
                             "WHERE h.account=? AND h.mode IN ('opening','bot','waiting','human','paused','blocked','returning') AND h.topic_state='new' AND h.topic_id IS NULL "
                             "ORDER BY h.updated LIMIT 1", (self.account,)).fetchone()
            if not row:
                return False
            route = dict(row)
            db.execute("UPDATE handoffs SET topic_state='creating',group_id=? WHERE account=? AND thread=?",
                       (self.config["group_id"], self.account, route["thread"]))
        try:
            topic = self.api.call("createForumTopic", {"chat_id": self.config["group_id"], "name": route["title"][:100]})
        except TelegramError as error:
            with self.store.db() as db:
                db.execute("UPDATE handoffs SET topic_state=? WHERE account=? AND thread=?",
                           ("new" if error.code in (401, 403, 429) else "failed" if error.rejected else "uncertain", self.account, route["thread"]))
            if not error.rejected:
                self.output("Тему для діалогу %s треба перевірити вручну. У правильній темі введи /bind %s." % (route["thread"], route["thread"]))
            raise
        with self.store.db() as db:
            db.execute("UPDATE handoffs SET topic_id=?,topic_state='ready',mode=CASE WHEN mode='opening' THEN 'human' ELSE mode END,updated=? WHERE account=? AND thread=?",
                       (topic["message_thread_id"], time.time(), self.account, route["thread"]))
        self.handoffs.compact_pending(self.account, route["thread"])
        group = str(self.config["group_id"])
        if group.startswith("-100"):
            url = "https://t.me/c/%s/%s" % (group[4:], topic["message_thread_id"])
            self.registry.tell(admin_owner(self.config), "topic-opened:%s:%s:%s" % (self.account, route["thread"], topic["message_thread_id"]),
                               ("🙋 Потрібна твоя допомога. " if route.get("attention") else "💬 Діалог готовий. ") + route["title"],
                               {"inline_keyboard": [[{"text": "Відкрити діалог", "url": url}]]})
        self.next_send = time.monotonic() + 3.2
        return True

    def deliver(self):
        if time.monotonic() < self.next_send:
            return
        with self.store.db() as db:
            row = db.execute("SELECT o.*,h.group_id,h.topic_id,h.mode,h.control_message_id,h.topic_tail_id,h.attention,h.title FROM bridge_outbox o JOIN handoffs h "
                             "ON h.account=o.account AND h.thread=o.thread JOIN effective_ads a ON a.account=h.account AND a.advert=h.advert "
                             "WHERE o.account=? AND o.status='pending' "
                             "AND h.group_id=? AND h.topic_id IS NOT NULL AND "
                             "(h.mode!='bot' OR o.kind NOT IN ('customer','seller','attention','prompt') OR (o.kind='attention' AND h.attention=1)) AND "
                             "(h.mode!='bot' OR o.kind!='history' OR o.event_key LIKE 'manual-history:%' OR (h.attention=1 AND o.event_key LIKE 'start:%:problem:%')) AND "
                             "(h.mode IN ('human','paused','bot','waiting') OR (h.mode IN ('closed','blocked','returning') "
                             "AND o.kind IN ('notice','control','history'))) ORDER BY CASE WHEN o.kind='control' AND h.control_message_id IS NULL THEN 0 WHEN o.kind='customer' AND h.mode='human' THEN 1 WHEN o.kind IN ('notice','prompt') THEN 2 WHEN o.kind='control' THEN 3 ELSE 4 END,o.id LIMIT 1",
                             (self.account, self.config["group_id"])).fetchone()
            if not row:
                return
            item = dict(row)
            db.execute("UPDATE bridge_outbox SET status='sending' WHERE id=? AND status='pending'", (item["id"],))
        fields = {"chat_id": item["group_id"], "message_thread_id": item["topic_id"],
                  "disable_notification": not (item["kind"] == "customer" and item["mode"] == "human")}
        try:
            if item["kind"] == "history":
                text, entities = quote_entities(json.loads(item["text"]))
                fields.update(text=text, entities=entities)
            elif item["kind"] in ("customer", "seller", "owner"):
                text, entities = quote_entities([item["text"]])
                fields.update(text=text, entities=entities)
            else:
                fields["text"] = item["text"]
            if item["kind"] == "control":
                live = self.handoffs.get(self.account, item['thread'])
                header = status_text(live['mode'], live)
                with self.store.db() as db:
                    pending = db.execute("SELECT COUNT(*) FROM bridge_replies WHERE account=? AND thread=? AND kind='reply' AND status='queued'", (self.account, item['thread'])).fetchone()[0]
                    in_flight = db.execute("SELECT 1 FROM drafts WHERE account=? AND thread=? AND status='sending' LIMIT 1", (self.account, item['thread'])).fetchone()
                if pending:
                    header += '\n⏳ Ручних повідомлень у черзі: %s.' % pending
                if live.get('attention'):
                    header += '\n🙋 Є звернення, яке ще потребує твоєї відповіді.'
                if in_flight and live['mode'] == 'human':
                    header += '\nОстання відповідь auto вже надсилається; наступні — за тобою.'
                # Preserve actionable rejection/uncertain-send diagnostics supplied by the worker.
                if live['mode'] == 'blocked' and item['text'] not in header:
                    header += '\n' + item['text']
                fields["text"] = item['title'] + '\n\n' + header
                fields["reply_markup"] = self.controls(live["mode"], self.orders.get(self.account, item["thread"]))
            # Every new topic message becomes the sole control anchor. Previous
            # message keyboards are retired by an idempotent, durable edit queue.
            current = self.handoffs.get(self.account, item['thread'])
            fields['reply_markup'] = self.controls(current['mode'])
            fields["link_preview_options"] = {"is_disabled": True}
            if item["kind"] == "control" and item["control_message_id"] and item['control_message_id'] == item['topic_tail_id']:
                try:
                    edit_fields = {key: value for key, value in fields.items() if key not in ('message_thread_id','disable_notification')}
                    self.api.call("editMessageText", dict(edit_fields, message_id=item["control_message_id"]))
                    result = {"message_id": item["control_message_id"]}
                except TelegramError as error:
                    if error.reason == "not_modified":
                        result = {"message_id": item["control_message_id"]}
                    elif error.reason == "message_missing":
                        result = self.api.call("sendMessage", fields)
                    else:
                        raise
            else:
                try:
                    result = self.api.call("sendMessage", fields)
                except TelegramError as error:
                    if error.reason != "entities" or not fields.get("entities"):
                        raise
                    # Telegram explicitly rejected the first POST. Plain text retains role labels.
                    plain = dict(fields)
                    plain.pop("entities", None)
                    result = self.api.call("sendMessage", plain)
        except TelegramError as error:
            if error.reason in ("topic_missing", "topic_closed"):
                try:
                    if self.repair_topic(item, error):
                        return
                except TelegramError as repair_error:
                    with self.store.db() as db:
                        db.execute("UPDATE bridge_outbox SET status='pending' WHERE id=?", (item["id"],))
                    raise repair_error
            with self.store.db() as db:
                db.execute("UPDATE bridge_outbox SET status=? WHERE id=?", ("pending" if error.code in (401, 403, 429) else "failed" if error.rejected else "uncertain", item["id"]))
            raise
        with self.store.db() as db:
            db.execute("UPDATE bridge_outbox SET status='sent',message_id=? WHERE id=?", (result["message_id"], item["id"]))
            db.execute("INSERT OR IGNORE INTO bridge_refs VALUES(?,?,?,?,?)",
                       (item["group_id"], result["message_id"], self.account, item["thread"], item["kind"]))
            if item["kind"] == "control":
                db.execute("UPDATE handoffs SET control_message_id=? WHERE account=? AND thread=?",
                           (result["message_id"], self.account, item["thread"]))
            self.topic_controls.attached(item, result['message_id'])
        self.topic_controls.cleanup()
        self.next_send = time.monotonic() + 3.2

    def deliver_private(self):
        owner = admin_owner(self.config)
        if owner is None or time.monotonic() < self.next_private_send:
            return
        with self.store.db() as db:
            row = db.execute("SELECT * FROM admin_outbox WHERE account=? AND owner=? AND status='pending' ORDER BY id LIMIT 1",
                             (self.account, owner)).fetchone()
            if not row:
                return
            item = dict(row)
            db.execute("UPDATE admin_outbox SET status='sending' WHERE id=? AND status='pending'", (item["id"],))
        fields = {"chat_id": owner, "text": item["text"], "link_preview_options": {"is_disabled": True}}
        keyboard = json.loads(item["keyboard"])
        if keyboard:
            fields["reply_markup"] = keyboard
        panel = item['event_key'].startswith('operator-panel:')
        panel_key = 'operator-panel-message:%s:%s' % (self.account, owner)
        previous = self.store.meta(panel_key).get('message_id') if panel else None
        if panel:
            fields['disable_notification'] = True
        try:
            if previous:
                try:
                    edit_fields = {key: value for key, value in fields.items() if key != 'disable_notification'}
                    result = self.api.call('editMessageText', dict(edit_fields, message_id=previous))
                except TelegramError as error:
                    if error.reason == 'not_modified':
                        result = {'message_id': previous}
                    elif error.reason == 'message_missing':
                        result = self.api.call('sendMessage', fields)
                    else:
                        raise
            else:
                result = self.api.call("sendMessage", fields)
        except TelegramError as error:
            with self.store.db() as db:
                db.execute("UPDATE admin_outbox SET status=? WHERE id=?", ("pending" if error.code in (401, 429) else "failed" if error.rejected else "uncertain", item["id"]))
            # An owner who has not opened the bot must not stop group forwarding.
            if error.code == 403:
                self.output("Відкрий Repliq особисто та натисни /start для приватного керування.")
                return
            raise
        with self.store.db() as db:
            db.execute("UPDATE admin_outbox SET status='sent' WHERE id=?", (item["id"],))
            if panel and isinstance(result, dict) and result.get('message_id'):
                self.store.set_meta(panel_key, {'message_id': result['message_id']})
        self.next_private_send = time.monotonic() + 1.1

    def private_update(self, update):
        query = update.get("callback_query")
        message = query.get("message", {}) if isinstance(query, dict) else update.get("message", {})
        sender = query.get("from", {}) if isinstance(query, dict) else message.get("from", {})
        chat = message.get("chat", {})
        owner = admin_owner(self.config)
        if chat.get("type") != "private":
            return False
        if (owner is None or sender.get("id") != owner or chat.get("id") != owner or sender.get("is_bot") or
                message.get("forward_origin") or message.get("forward_date") or message.get("via_bot") or message.get("sender_chat")):
            return True
        self.last_private_activity = time.monotonic()
        key = "admin:%s:%s" % (self.scope, update["update_id"])
        if isinstance(query, dict):
            data = str(query.get("data", ""))
            if data.startswith("openchat:"):
                route = self.handoffs.request_topic(self.account, data.split(":", 1)[1], key)
                answer = ('Тему треба звірити вручну: результат попереднього створення невідомий.' if route and route['topic_state'] == 'uncertain' else
                          'Готую тему цього клієнта…' if route else 'Діалог уже неактуальний')
                self.api.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": answer})
                return True
            if data.startswith("operator:"):
                from operator_menu import OperatorMenu
                parts = data.split(":")
                if len(parts) == 3 and parts[2].isdigit():
                    if parts[1] == "payment":
                        self.registry.tell(owner, key, "Перевірка оплати тепер у Ledgerly, у картці замовлення.")
                    else:
                        OperatorMenu(self.handoffs, self.registry, self.account, self.config["group_id"]).show(owner, key, parts[1], int(parts[2]))
                try:
                    self.api.call("answerCallbackQuery", {"callback_query_id": query["id"]})
                except TelegramError:
                    pass
                return True
            if data.startswith("catalog:"):
                parts = data.split(":")
                if len(parts) == 3 and parts[1] in ("edit_ad", "remove_ad") and parts[2].isdigit():
                    self.registry.private_message(owner, "/%s %s" % (parts[1], parts[2]), key)
                try:
                    self.api.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Готую…"})
                except TelegramError:
                    pass
                return True
            if data.startswith("product:"):
                selected = self.registry.select(owner, data.split(":", 1)[1], key)
                try:
                    self.api.call("answerCallbackQuery", {"callback_query_id": query["id"],
                                  "text": "Готую…" if selected else "Вибір застарів. Повтори команду."})
                except TelegramError:
                    pass
            return True
        if not 0 <= time.time() - message.get("date", 0) <= 900:
            return True
        text = str(message.get("text") or "").strip()
        if text in ("💬 Діалоги", "🙋 Потрібен я", "💳 Стан mono", "💳 Перевірити оплату"):
            if text == "💳 Перевірити оплату":
                self.registry.tell(owner, key, "Оплату перевіряєш у Ledgerly, у картці замовлення. Тут — лише консультації та перемикання «Я / Бот».")
                return True
            from operator_menu import OperatorMenu
            OperatorMenu(self.handoffs, self.registry, self.account, self.config["group_id"]).show(owner, key, 'mono' if text == '💳 Стан mono' else "needs" if text.startswith("🙋") else "all")
            return True
        if text.startswith("/") and "@" in text.split(maxsplit=1)[0]:
            target = text.split(maxsplit=1)[0].split("@", 1)[1]
            if target.casefold() != getattr(self, "me", {}).get("username", "").casefold():
                return True
        if text:
            self.registry.private_message(owner, text[:4000], key)
        return True

    def bind_topic(self, message, thread_id):
        route = self.handoffs.get(self.account, thread_id)
        if not route or route["topic_id"] is not None or route["mode"] != "opening":
            return
        with self.store.db() as db:
            taken = db.execute("SELECT 1 FROM handoffs WHERE group_id=? AND topic_id=?",
                               (self.config["group_id"], message["message_thread_id"])).fetchone()
            if taken:
                return
            db.execute("UPDATE handoffs SET group_id=?,topic_id=?,topic_state='ready',mode='human' WHERE account=? AND thread=?",
                       (self.config["group_id"], message["message_thread_id"], self.account, thread_id))

    def clear_update(self, update):
        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        if not is_clear(text):
            return False
        sender, chat = message.get("from", {}), message.get("chat", {})
        owner = admin_owner(self.config)
        if (not owner or sender.get("id") != owner or sender.get("is_bot") or
                chat.get("id") not in (owner, self.config["group_id"]) or
                chat.get("type") not in ("private", "group", "supergroup") or
                any(message.get(key) for key in ("sender_chat", "forward_origin", "forward_date", "via_bot")) or
                not 0 <= time.time() - message.get("date", 0) <= 900):
            return True
        command = text.split(maxsplit=1)[0]
        if "@" in command and command.split("@", 1)[1].casefold() != getattr(self, "me", {}).get("username", "").casefold():
            return True
        fields = {"chat_id": chat["id"]}
        if message.get("message_thread_id"):
            fields["message_thread_id"] = message["message_thread_id"]
        if len(text.split(maxsplit=1)) > 1:
            notice = "Щоб очистити повідомлення тут, напиши просто /clear."
        elif not self.history:
            notice = "Очищення тимчасово недоступне. Звичайна робота бота продовжується."
        else:
            if chat["id"] == self.config["group_id"]:
                member = self.api.call("getChatMember", {"chat_id": chat["id"], "user_id": self.me["id"]})
                if member.get("status") != "creator" and not (member.get("status") == "administrator" and member.get("can_delete_messages")):
                    self.api.call("sendMessage", dict(fields, text="Надай Repliq право адміністратора «Видалення повідомлень», потім повтори /clear."))
                    return True
            self.history.remember(message)
            try:
                notice = self.history.clear(message, lambda chat_id, ids: self.api.call("deleteMessages", {"chat_id": chat_id, "message_ids": ids}))
            except sqlite3.Error:
                notice = "Не вдалося завершити очищення. Спробуй /clear ще раз. Замовлення збережені."
        self.api.call("sendMessage", dict(fields, text=notice))
        if chat['id'] == self.config['group_id'] and message.get('message_thread_id'):
            route = self.handoffs.route(chat['id'], message['message_thread_id'], self.account)
            if route:
                self.handoffs.control(self.account, route['thread'], status_text(route['mode']), 'after-clear:%s:%s' % (self.scope, update['update_id']))
        return True

    def update(self, update):
        if self.clear_update(update):
            return
        if self.private_update(update):
            return
        if isinstance(update.get("callback_query"), dict):
            self.chat_callback(update)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        sender, chat = message.get("from", {}), message.get("chat", {})
        if (sender.get("id") not in self.config["owner_ids"] or sender.get("is_bot") or message.get("sender_chat") or
                chat.get("id") != self.config["group_id"] or not message.get("message_thread_id") or
                message.get("forward_origin") or message.get("forward_date") or message.get("via_bot")):
            return
        # Old operator text is not permission to send it much later.
        if not 0 <= time.time() - message.get("date", 0) <= 900:
            return
        text = str(message.get("text") or "").strip()
        if text.startswith("/") and "@" in text.split(maxsplit=1)[0]:
            target = text.split(maxsplit=1)[0].split("@", 1)[1]
            if target.casefold() != getattr(self, "me", {}).get("username", "").casefold():
                return
        command = text.split(maxsplit=1)[0].split("@")[0].casefold() if text else ""
        arguments = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) == 2 else ""
        if command == "/bind" and arguments:
            self.bind_topic(message, arguments.strip())
            return
        route = self.handoffs.route(chat["id"], message["message_thread_id"], self.account)
        if not route:
            return
        self.topic_controls.owner_message(route, message['message_id'])
        self.handoffs.control(self.account, route['thread'], status_text(route['mode']),
                              'owner-tail:%s:%s' % (self.scope,message['message_id']))
        key = "%s:%s" % (self.scope, update["update_id"])
        if command == "/done":
            self.return_to_bot(route, sender["id"], "command:" + key)
            return
        modes = {"/cancel": "closed", "/pause": "paused", "/resume": "human"}
        if command in modes:
            self.handoffs.mode(self.account, route["thread"], modes[command])
            self.handoffs.control(self.account, route["thread"], status_text(modes[command]), "command:" + key)
            return
        if command == "/history":
            self.handoffs.reply(key, route, sender["id"], "", "history")
            return
        ref = message.get("reply_to_message", {}).get("message_id")
        if ref:
            with self.store.db() as db:
                source = db.execute("SELECT account,thread FROM bridge_refs WHERE group_id=? AND message_id=?",
                                    (chat["id"], ref)).fetchone()
            if source and (source["account"], source["thread"]) != (self.account, route["thread"]):
                return
        if any(message.get(kind) for kind in ('photo', 'document', 'voice', 'video', 'animation', 'sticker', 'audio')):
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                if not db.execute('INSERT OR IGNORE INTO bridge_actions VALUES(?,?)', ('reply:' + key, time.time())).rowcount:
                    return
                if route['mode'] in ('bot', 'human', 'waiting', 'returning'):
                    self.handoffs.take(self.account, route['thread'], 'media:' + key)
                self.handoffs.notice(self.account, route['thread'], 'Вкладення не надіслано в OLX. Надішли його безпосередньо в застосунку OLX; тут можна відповідати текстом.', 'unsupported-media:' + key)
            return
        if not text or text.startswith("/") and command != "/say":
            return
        if command == "/say":
            text = arguments.strip()
        if not 1 <= len(text.strip()) <= 2000:
            self.handoffs.notice(self.account, route["thread"], "Відповідь має містити 1–2000 символів. Не надіслано.", "invalid:" + key)
            return
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            live = self.handoffs.get(self.account, route['thread'])
            if not live or (live['group_id'], live['topic_id'], live['buyer'], live['advert']) != (route['group_id'], route['topic_id'], route['buyer'], route['advert']):
                return
            if not db.execute('INSERT OR IGNORE INTO bridge_actions VALUES(?,?)', ('reply:' + key, time.time())).rowcount:
                return  # A retried Telegram update must not reclaim a chat returned to auto.
            if live['mode'] not in ('bot', 'human', 'waiting', 'returning'):
                self.handoffs.notice(self.account, route['thread'], 'Не надіслано. Спочатку натисни «я», щоб продовжити цей діалог. Текст залишився вище.', 'inactive-reply:' + key)
                return
            route = self.handoffs.take(self.account, route['thread'], 'write:' + key) if live['mode'] != 'human' else live
            self.handoffs.reply(key, route, sender["id"], text.strip())
            self.handoffs.renew_human(self.account, route["thread"])
            self.handoffs.control(self.account, route['thread'], status_text('human'), 'queued:' + key)

    def return_to_bot(self, route, owner, key, reset=True):
        if route["mode"] == "bot" and not reset:
            self.handoffs.mode(self.account, route["thread"], "bot")
            self.handoffs.control(self.account, route["thread"], status_text('bot'), 'acknowledged:' + key)
            return
        self.handoffs.mode(self.account, route["thread"], "returning", drain=True)
        self.store.invalidate(self.account, route["thread"], "Повернення діалогу боту")
        self.handoffs.reply(key, route, owner, "", "return_bot" if reset else "switch_bot")
        self.handoffs.control(self.account, route["thread"], status_text("returning"), "control:" + key)

    def chat_callback(self, update):
        query = update["callback_query"]
        message, sender = query.get("message", {}), query.get("from", {})
        chat = message.get("chat", {})
        if (sender.get("id") not in self.config["owner_ids"] or sender.get("is_bot") or
                chat.get("id") != self.config["group_id"]):
            return
        with self.store.db() as db:
            trusted = db.execute("SELECT thread FROM bridge_refs WHERE group_id=? AND message_id=? AND account=?",
                                 (chat["id"], message.get("message_id"), self.account)).fetchone()
        if not trusted:
            return
        route = self.handoffs.get(self.account, trusted["thread"])
        if not route or route["group_id"] != chat["id"] or message.get("message_thread_id", route["topic_id"]) != route["topic_id"]:
            return
        data = str(query.get("data", ""))
        if not data.startswith("chat:"):
            return
        action = data[len("chat:"):]
        payment = action.split(":") if action.startswith("pay:") else None
        if action not in ("done", "ask_done", "bot", "take", "pause", "resume", "history", "reply", "more", "less", "order") and not (payment and len(payment) == 4 and payment[-1] in ("paid", "unpaid")):
            return
        key = "button:%s:%s" % (self.scope, update["update_id"])
        answer, custom_markup = "Готово", None
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            first = db.execute("INSERT OR IGNORE INTO bridge_actions VALUES(?,?)", (key, time.time())).rowcount
            if first:
                route = self.handoffs.get(self.account, route["thread"])
                if payment:
                    answer = "Перевірка оплати тепер лише в Ledgerly, у картці замовлення."
                elif action in ("more", "less"):
                    custom_markup = self.controls(route["mode"], self.orders.get(self.account, route["thread"]), expanded=action == "more")
                    answer = "Дії з цим діалогом" if action == "more" else "Панель згорнуто"
                elif action == "ask_done":
                    custom_markup = self.controls(route['mode'])
                    answer = "Панель оновлено: auto / я"
                elif action == "order":
                    self.handoffs.notice(self.account, route["thread"], self.orders.details(self.account, route["thread"]), "order-details:" + key)
                    answer = "Покажу замовлення в цій темі"
                elif action in ("done", "bot"):
                    self.return_to_bot(route, sender["id"], key, reset=action == "done")
                    answer = "Бот уже працює" if action == "bot" and route["mode"] == "bot" else "Повертаю діалог боту"
                elif action == "history":
                    self.handoffs.reply(key, route, sender["id"], "", "history")
                    answer = "Надішлю історію сюди"
                elif action == "reply":
                    if route["mode"] in ("bot", "human", "waiting", "paused"):
                        if route["mode"] != "human":
                            self.handoffs.take(self.account, route['thread'], key)
                        self.handoffs.enqueue(db, "reply-prompt:" + key, self.account, route["thread"], "prompt",
                                             "✉️ Напиши відповідь у цій темі — надішлю її клієнту в OLX.")
                        answer = "Бот мовчить. Напиши відповідь клієнту в цій темі."
                    else:
                        answer = "Спочатку продовж діалог або візьми його собі"
                elif action == "take" and route["mode"] in ("bot", "human", "waiting", "paused", "returning", "closed", "blocked"):
                    self.handoffs.take(self.account, route['thread'], key)
                    answer = "Відповідаєш ти. Пиши в цій темі — надішлю клієнту."
                elif action == "pause" and route["mode"] in ("human", "bot", "waiting") or action == "resume" and route["mode"] == "paused":
                    mode = "paused" if action == "pause" else "human"
                    self.handoffs.mode(self.account, route["thread"], mode)
                    self.handoffs.control(self.account, route["thread"], status_text(mode), key)
                else:
                    answer = "Стан діалогу вже змінився"
        try:
            self.api.call("answerCallbackQuery", {"callback_query_id": query["id"], "text": answer, "show_alert": action == "ask_done"})
            current = self.handoffs.get(self.account, route["thread"])
            self.topic_controls.refresh(current, custom_markup or self.controls(current['mode']))
            if current.get('keyboard_message_id') != message['message_id']:
                with self.store.db() as db:
                    db.execute('INSERT OR IGNORE INTO bridge_markup_cleanup(group_id,message_id,account) VALUES(?,?,?)',
                               (chat['id'], message['message_id'], self.account))
        except TelegramError:
            pass

    def run(self):
        try:
            self._run()
        finally:
            self.health("stopped")
            self.store.close_current()
            if self.history:
                self.history.close()

    def consume_update(self, update):
        ident = update.get("update_id")
        if type(ident) is not int:
            return
        if self.history:
            self.history.observe_update(update)
        try:
            self.update(update)
        except TelegramError:
            raise
        except Exception as error:
            attempts = self.update_failures.get(ident, 0) + 1
            self.update_failures = {ident: attempts}
            frames = traceback.extract_tb(error.__traceback__)
            origin = "%s:%s" % (Path(frames[-1].filename).name, frames[-1].lineno) if frames else "update"
            self.output("Telegram: не оброблено дію (%s, %s); спроба %s/3." % (type(error).__name__, origin, attempts))
            if attempts < 3:
                raise
            owner = admin_owner(self.config)
            if owner:
                self.registry.tell(owner, "failed-update:%s:%s" % (self.scope, ident),
                                   "Не вдалося виконати останню дію. Відкрий меню або натисни потрібну кнопку ще раз. OLX-відповіді в автоматичному режимі продовжуються.")
        self.store.set_meta(self.offset_key, {"value": ident + 1})
        self.update_failures.pop(ident, None)

    def fast_admin(self):
        # Pure local catalog/editor work must not queue behind OLX histories.
        # Writes requiring a runtime catalog swap stay in the main worker.
        worker = self.admin_worker
        if worker is None:
            return
        from catalog_admin import process_admin
        allowed = ['sizing_list', 'sizing_open', 'sizing_facts', 'sizing_edit', 'sizing_preview']
        if self.registry.products():
            allowed.append('products')
        for _ in range(4):
            if not self.registry.next_job(allowed=allowed):
                break
            process_admin(worker, allowed=allowed)

    def _run(self):
        delay, bootstrapped = 5, False
        while not self.stop.is_set():
            phase = "bootstrap" if not bootstrapped else "updates"
            try:
                if not bootstrapped:
                    self.bootstrap()
                    bootstrapped = True
                    self.output("Telegram: групу й власників перевірено. Передані діалоги мають окремі теми.")
                self.fast_admin()
                # Private UI has an independent queue; a broken group topic
                # must not prevent the owner's catalog/menu from arriving.
                try:
                    self.deliver_private()
                except TelegramError as error:
                    self.report_error(error, 'private_ui')
                if time.monotonic() >= self.next_delivery:
                    for step in (self.create_topic, self.deliver, self.sync_topic_name, self.topic_controls.cleanup):
                        phase = step.__name__
                        try:
                            step()
                        except TelegramError as error:
                            self.report_error(error, phase)
                            wait = max(60 if error.code in (401, 403) else 3 if error.code == 400 else 5, min(600, error.retry_after))
                            self.next_delivery = time.monotonic() + wait
                            self.health("retrying", error.code, error.reason, phase, time.time() + wait)
                            break
                phase = "getUpdates"
                offset = self.store.meta(self.offset_key)["value"]
                with self.store.db() as db:
                    active = db.execute("SELECT 1 FROM handoffs WHERE account=? AND mode IN ('waiting','opening','human','paused') LIMIT 1",
                                        (self.account,)).fetchone() is not None
                with self.store.db() as db:
                    queued = db.execute("SELECT 1 FROM bridge_outbox o JOIN handoffs h ON h.account=o.account AND h.thread=o.thread WHERE o.account=? AND o.status='pending' AND h.topic_id IS NOT NULL LIMIT 1", (self.account,)).fetchone() is not None
                updates = self.api.call("getUpdates", {"offset": offset, "limit": 20, "timeout": 1 if queued or time.monotonic() - self.last_private_activity < 40 else 2 if active else 5,
                                                      "allowed_updates": ["message", "callback_query"]})
                if self.polling_interrupted:
                    self.output('Telegram: з’єднання відновлено. Отримання команд продовжується.')
                    self.polling_interrupted = False
                for update in updates:
                    phase = "update"
                    self.consume_update(update)
                self.fast_admin()
                self.deliver_private()
                if time.monotonic() >= self.next_delivery:
                    self.health("ready")
                delay = 5
            except TelegramError as error:
                self.report_error(error, phase)
                wait = max(30 if error.code == 409 else delay, min(600, error.retry_after))
                self.health("retrying", error.code, error.reason, phase, time.time() + wait)
                if error.code in (401, 403):
                    bootstrapped = False
                self.stop.wait(wait)
                delay = min(120, delay * 2)
            except AppError as error:
                self.output(str(error))
                self.health("configuration", phase=phase, retry_at=time.time() + 60)
                bootstrapped = False
                self.stop.wait(60)
            except Exception as error:
                frames = traceback.extract_tb(error.__traceback__)
                origin = "%s:%s" % (Path(frames[-1].filename).name, frames[-1].lineno) if frames else phase
                self.output("Telegram: помилка %s у %s. Відновлюю з'єднання через %s с." % (type(error).__name__, origin, delay))
                self.health("retrying", reason=type(error).__name__, phase=phase, retry_at=time.time() + delay)
                self.stop.wait(delay)
                delay = min(120, delay * 2)
