"""One OLX network worker and an independent terminal approval queue."""

import argparse
import copy
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

from app import AppError, ROOT, Repliq, private_lock, read_json
from catalog import Catalog
from catalog_admin import process_admin, admin_owner
from chat_messages import candidate, count, ordered
from chat_store import Store
from buyer_identity import BuyerNames, topic_title
from olx_delivery import queue_alert
from dialogue import Dialogue
from handoff_store import Handoffs, status_text
from inventory import DemoInventory, Inventory, StockUnavailable, database_url, stock_signature, stock_rows
from olx_client import OlxClient, identifier
from polling import AdaptivePolling
from product_registry import Registry, runtime_catalog
from telegram_bridge import Bridge, settings as telegram_settings
from checkout import payment_settings, payment_signature
from shop_facts import shop_day, shipping_reply
from order_board import OrderBoard
from order_summary import restore_legacy_invoice, snapshot, invoice_key, confirmation_key
from customer_updates import CustomerUpdates, can_watch_shipment, shipped_waybill
from manual_session import selection_after_manual
from fulfillment import Fulfillment, parcel_payload
from fulfillment_queue import digest as parcel_digest
from monobank_payments import PaymentChecker, marker as bank_marker
from shipping_directory import ShippingDirectory
from customer_language import intents
from customer_phrasing import wants_dispatch_notice
from customer_feedback import feedback_kind
from catalog import normalize
from conversation_policy import requests_owner
from conversation_turn import history_excerpt
from advert_knowledge import AdvertDescriptions

PRINT_LOCK = threading.Lock()
WORKER_VERSION = '2026.11.19.4'


def clean(value):
    import unicodedata
    return "".join(c for c in str(value) if c in "\n\t" or not unicodedata.category(c).startswith("C"))


def say(text):
    with PRINT_LOCK:
        print(clean(text), flush=True)


def binding(thread):
    advert, buyer = thread.get("advert_id"), thread.get("interlocutor_id")
    if advert is None or buyer is None:
        raise AppError("OLX не передав оголошення або співрозмовника; перевір діалог вручну.")
    return str(advert), str(buyer)


def worker_status(store):
    report = dict(store.meta("worker"))
    if not report:
        return report
    report["recorded_status"] = report.get("status")
    try:
        with private_lock(store.path.parent / "responder-state.lock", blocking=False):
            report["status"] = "stopped"
    except BlockingIOError:
        if report.get("status") == "running" and time.time() - report.get("heartbeat", 0) > 120:
            report["status"] = "unresponsive"
    except OSError:
        report["status"] = "unknown"
    return report


class Worker:
    def __init__(self, client, store, catalog, catalog_path, inventory, stop=None, auto_send=False):
        self.client, self.store, self.catalog = client, store, catalog
        self.base_catalog = catalog
        self.catalog_path, self.inventory = Path(catalog_path), inventory
        self.dialogue = Dialogue(catalog, inventory, payment_settings(store.path.parent))
        self.descriptions = AdvertDescriptions(client)
        self.dialogue.description_source = self.descriptions
        self.stop = stop if stop is not None else threading.Event()
        self.fulfillment = Fulfillment(store.path.parent, self.stop, catalog, can_create=self.parcel_can_create)
        self.monobank = PaymentChecker(store.path.parent, self.fulfillment.queue, self.stop, report=say, health=self.bank_health)
        self.dialogue.address_book = ShippingDirectory(store.path.parent) if self.fulfillment.queue else None
        self.fulfillment_cursor = 0.0
        self.ready = threading.Event()
        self.failure = None
        self.auto_send = auto_send
        self.session, self.account = uuid.uuid4().hex, ""
        p = catalog.data["polling"]
        self.schedule = AdaptivePolling(p["idle_seconds"], p["active_seconds"], p["active_window_seconds"], p["max_active_chats"])
        self.counts, self.buffers = {}, {}
        self.started_at = time.time()
        self.last_heartbeat, self.last_cleanup = 0.0, 0.0
        self.retry_at, self.failures = 0.0, 0
        self.handoffs = Handoffs(store)
        self.orders = OrderBoard(store)
        self.customer_updates = CustomerUpdates(store)
        self.telegram_config = telegram_settings(store.path.parent)
        self.telegram_thread = None
        self.registry = None
        self.names = BuyerNames(client, store)
        self.next_product_sync = 0.0
        self.product_sync_error = None
        self.last_purge = None
        self.next_manual_check = 0.0

    def heartbeat(self, status="running"):
        self.store.set_meta("worker", {"account": self.account, "session": self.session, "heartbeat": time.time(),
                                       "status": status, "pid": os.getpid(), "auto_send": self.auto_send, "version": WORKER_VERSION})

    def bank_health(self, value):
        previous = self.store.meta('mono-health')
        if (previous.get('scope'),previous.get('activation_id')) != (value.get('scope'),value.get('activation_id')):
            previous = {}
        value = dict(value)
        if value.get('state') == 'retry':
            value['failure_since'] = previous.get('failure_since') or value['updated']
            threshold = 300 if value.get('code') in ('401','403') else 900
            if (value['updated'] - value['failure_since'] >= threshold and not previous.get('owner_alerted') and
                    getattr(self, 'registry', None) and admin_owner(self.telegram_config)):
                self.registry.tell(admin_owner(self.telegram_config), 'mono-problem:%s:%s' % (value.get('scope',''), value['failure_since']),
                                   '💳 Потрібна перевірка mono\n\nАвтоперевірка оплат тривалий час недоступна. Спроби продовжуються автоматично; консультації клієнтів працюють. За потреби перевір оплату в Ledgerly. Деталі — у «Стан mono».',
                                   {'inline_keyboard': [[{'text': '💳 Стан mono', 'callback_data': 'operator:mono:0'}]]})
                value['owner_alerted'] = True
        else:
            value.update(failure_since=0, owner_alerted=False)
        self.store.set_meta('mono-health', dict(previous, **value))

    def conversation_title(self, buyer, advert):
        product = self.catalog.products.get(self.catalog.adverts.get(str(advert)), {})
        name = self.names.cached(self.account, buyer) if self.telegram_config else ''
        return topic_title(name, buyer, product.get('name', 'OLX'))

    def refresh_buyer_names(self):
        result = self.names.refresh()
        if not result:
            return
        account, buyer, name = result
        if account != self.account:
            return
        for route in self.handoffs.all(account):
            if route['buyer'] != buyer:
                continue
            product = self.catalog.products.get(self.catalog.adverts.get(route['advert']), {})
            title = topic_title(name, buyer, product.get('name', 'OLX'))
            if self.handoffs.title(account, route['thread'], title):
                self.handoffs.control(account, route['thread'], status_text(route['mode']), 'buyer-name:%s:%s:%s' % (account, route['thread'], title))

    def delivery_alerts(self, thread, buyer, advert, fresh_messages):
        if not self.telegram_config or not self.registry:
            return
        context = self.store.context(self.account, thread, buyer, advert, self.catalog.data['limits']['memory_days'])
        product = self.catalog.products.get(self.catalog.adverts.get(advert), {})
        queue_alert(self.registry, self.telegram_config, self.account, thread, buyer, advert, context,
                    fresh_messages, product.get('name', 'OLX'), self.names.cached(self.account, buyer))
        self.last_heartbeat = time.monotonic()

    def merge_order_updates(self, context, live):
        """Preserve payment and shipment updates that arrived after a draft was prepared."""
        if (context.get("invoice_id") and context.get("invoice_id") == live.get("invoice_id") and
                invoice_key(snapshot(self.catalog, context)) == invoice_key(snapshot(self.catalog, live))):
            for key in ("payment_verified", "payment_verified_by", "verified_invoice_key", "parcel", "shipment_notice_invoice"):
                if key in live:
                    context[key] = live[key]
        return context

    def save_dialogue_context(self, thread, buyer, advert, context):
        with self.store.db() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            live = self.store.context(self.account, thread, buyer, advert, self.catalog.data["limits"]["memory_days"])
            context = self.merge_order_updates(context, live)
            self.store.save_context(self.account, thread, buyer, advert, context)
        return context

    def invoice_context(self, thread, buyer, advert, invoice):
        return self.store.order_context(self.account, thread, buyer, advert, invoice, self.catalog.data["limits"]["memory_days"])

    def resume_candidate(self, route, history, metadata):
        """Only unanswered messages received during this manual session can be resumed."""
        since = route.get("human_since", 0)
        if not since:
            return
        suspended = set(self.store.meta('manual-pending:%s:%s' % (self.account, route['thread'])).get('source_ids', []))
        allowed = {m["_id"] for m in history if m.get("type") == "received" and (m["_time"] >= since or m['_id'] in suspended)}
        with self.store.db() as db:
            answered = {r[0] for r in db.execute("SELECT s.value FROM drafts d,json_each(d.payload,'$.source_ids') s WHERE d.account=? AND d.thread=? "
                                                "AND d.status IN ('sent','sending','uncertain') AND s.value IN (SELECT value FROM json_each(?))",
                                                (self.account, route['thread'], json.dumps(list(allowed))))}
        allowed.difference_update(answered)
        fresh = candidate(history, allowed)
        if fresh:
            with self.store.db() as db:
                db.executemany('DELETE FROM handled WHERE account=? AND thread=? AND message=?',
                               [(self.account, route['thread'], source) for source in fresh['source_ids']])
            self.buffers[route["thread"]] = dict(fresh, buyer=route["buyer"], advert=route["advert"],
                                                  recent_history=history_excerpt(history, fresh['source_ids']), due=time.monotonic())
            self.schedule.activate(route["thread"])

    def merge_manual_choices(self, route, history):
        previous = self.store.context(self.account, route['thread'], route['buyer'], route['advert'], self.catalog.data['limits']['memory_days'])
        current = selection_after_manual(self.catalog, previous, history, route.get('human_since', 0))
        if current != previous:
            self.save_dialogue_context(route['thread'], route['buyer'], route['advert'], current)

    def resume_expired(self):
        if time.monotonic() < self.next_manual_check:
            return
        self.next_manual_check = time.monotonic() + 10
        for route in self.handoffs.expired_humans(self.account):
            if route["advert"] not in self.catalog.adverts:
                continue
            metadata = self.client.thread(route["thread"])
            if binding(metadata) != (route["advert"], route["buyer"]):
                self.handoffs.mode(self.account, route["thread"], "blocked")
                continue
            history = ordered(self.client.messages(route["thread"]), route["thread"], self.catalog.timezone)
            if count(metadata) != len(history):
                continue
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                live = self.handoffs.get(self.account, route["thread"])
                if live["mode"] not in ("human", "opening") or live["human_until"] > time.time():
                    continue
                self.merge_manual_choices(live, history)
                self.handoffs.mode(self.account, route["thread"], "bot")
                if live.get('attention'):
                    db.execute('UPDATE handoffs SET attention=1 WHERE account=? AND thread=?', (self.account, route['thread']))
                self.handoffs.advance(self.account, route["thread"], len(history))
                self.handoffs.control(self.account, route["thread"], status_text("bot"), "auto-resume:%s:%s:%s" % (self.account, route["thread"], route["human_until"]))
            self.counts[route["thread"]] = len(history)
            self.resume_candidate(route, history, metadata)

    def initialize(self):
        profile = self.client.identity()
        self.account = str(profile["id"])
        self.registry = Registry(self.store, self.account)
        owner = admin_owner(self.telegram_config)
        if owner:
            self.registry.tell(owner, "operator-interface:v11:%s:%s" % (self.account, owner),
                               "Repliq · консультації OLX\n\n🙋 Потрібен я — лише звернення, де потрібна допомога.\n💬 Діалоги — усі клієнти.\n"
                               "Напиши в темі клієнта — підключишся особисто. «auto» повертає бота. Через 40 хв без твоїх відповідей він повертається сам. 💳 Стан mono — діагностика оплат.\nОплату й відправлення ведеш у Ledgerly.", self.registry.menu())
        with self.store.db() as db:
            db.execute("UPDATE admin_outbox SET status='uncertain' WHERE account=? AND status='sending'", (self.account,))
        self.refresh_products()
        with self.store.db() as db:
            interrupted_auto = [dict(row) for row in db.execute("SELECT thread,payload FROM drafts WHERE account=? AND status='sending'", (self.account,))]
        self.store.begin(self.account, self.session)
        self.handoffs.recover(self.account, interrupted_auto)
        self.handoffs.customer_requests_only(self.account)
        self.customer_updates.recover()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            restored_cards = []
            for route in self.handoffs.all(self.account):
                if route["advert"] not in self.catalog.adverts or route["mode"] == "closed":
                    continue
                self.handoffs.title(self.account, route['thread'], self.conversation_title(route['buyer'], route['advert']))
                context = self.store.context(self.account, route["thread"], route["buyer"], route["advert"], self.catalog.data["limits"]["memory_days"])
                if context:
                    restored = restore_legacy_invoice(self.catalog, context)
                    if restored is not context:
                        self.store.save_context(self.account, route["thread"], route["buyer"], route["advert"], restored)
                    self.monobank.watch_invoice(self.account, route['thread'], restored, self.catalog)
                    revision = self.orders.sync(self.account, route["thread"], restored, self.catalog)
                    if revision is not None:
                        restored_cards.append((route, revision))
            self.handoffs.refresh_controls(self.account)
            for route, revision in restored_cards:
                self.handoffs.control(self.account, route["thread"], status_text(route["mode"]),
                                      "order-restored:%s:%s:%s" % (self.session, route["thread"], revision))
        threads = self.client.threads()
        self.counts = {identifier(t).casefold(): count(t) for t in threads}
        for route in self.handoffs.all(self.account):
            if route["mode"] in ("waiting", "opening", "human", "paused"):
                self.counts[route["thread"]] = route["last_count"]
        self.started_at = time.time()
        self.schedule.inbox_checked()
        if self.handoffs.all(self.account):
            self.schedule.next_inbox_at = time.monotonic()
        self.heartbeat()
        with self.store.db() as db:
            db.execute("UPDATE admin_jobs SET status='queued' WHERE account=? AND status='working'", (self.account,))
        self.ready.set()
        if self.telegram_config:
            self.telegram_thread = Bridge(self.handoffs, self.telegram_config, self.account, self.stop, say)
            self.telegram_thread.admin_worker = self
            self.telegram_thread.start()
        mode = "Відповідаю автоматично. Керування переданими діалогами — кнопками в Telegram." if self.auto_send else "Відповіді надсилаються після твого підтвердження."
        say("Repliq %s · %s · %s діалогів зафіксовано без читання старих повідомлень.\nЧекаю нові повідомлення. %s\nЗалиш це вікно відкритим, поки бот працює. Ctrl+C — зупинити." % (WORKER_VERSION, profile.get("name", "OLX"), len(threads), mode))
        manual = sum(r["mode"] in ("waiting", "opening", "human", "paused") for r in self.handoffs.all(self.account))
        if manual:
            say("%s діалогів у ручному режимі. «auto» повертає відповіді одразу; через 40 хвилин без твоїх дій — автоматично." % manual)
        for item in self.store.report():
            if item["account"] == self.account:
                say("Потрібна ручна перевірка OLX: діалог %s, результат попереднього надсилання невідомий." % item["thread"])

    def compose_products(self):
        if self.store.meta("products-sync:" + self.account).get("valid"):
            catalog = runtime_catalog(self.base_catalog, self.registry.products(), self.registry.bindings(),
                                      self.store.meta('product-keys:' + self.account), self.registry.sizing_profiles())
            self.store.set_meta('product-keys:' + self.account,
                                {p['ledgerly_product_id']:p['key'] for p in catalog.products.values()})
        else:
            catalog = Catalog(copy.deepcopy(self.base_catalog.data))
            for binding_row in self.registry.bindings():
                if not binding_row["active"]:
                    catalog.adverts.pop(binding_row["advert"], None)
        self.catalog = catalog
        self.dialogue.catalog = catalog
        self.fulfillment.catalog = catalog
        self.registry.publish(catalog)

    def refresh_products(self, force=False):
        self.next_product_sync = time.monotonic() + 60
        if not hasattr(self.inventory, "catalogue"):
            self.compose_products()
            return
        try:
            products = self.inventory.catalogue()
            self.registry.snapshot(products)
            self.compose_products()
            self.product_sync_error = None
        except StockUnavailable as error:
            self.compose_products()
            if str(error) != self.product_sync_error:
                say("Каталог Ledgerly: " + str(error))
            self.product_sync_error = str(error)
            if force:
                raise

    def baseline_ad(self, advert):
        was_allowed = str(advert) in self.catalog.adverts
        for metadata in self.client.threads(advert_id=int(advert)):
            if str(metadata.get("advert_id")) != str(advert):
                raise AppError("OLX змінив фільтр оголошення; прив'язку не збережено.")
            thread_id, current = identifier(metadata).casefold(), count(metadata)
            route = self.handoffs.get(self.account, thread_id)
            if was_allowed and route and route["mode"] not in ("closed", "blocked"):
                # Editing a live advert must not discard a human operator's unread updates.
                self.counts[thread_id] = route["last_count"]
            else:
                self.counts[thread_id] = current
                if route and current is not None:
                    self.handoffs.advance(self.account, thread_id, current)

    def invalidate_ad(self, advert):
        with self.store.db() as db:
            threads = [r[0] for r in db.execute("SELECT thread FROM chats WHERE account=? AND advert=?", (self.account, str(advert)))]
        for thread_id in threads:
            self.store.invalidate(self.account, thread_id, "Змінено налаштування оголошення")
        for thread_id, buffer in list(self.buffers.items()):
            if buffer["advert"] == str(advert):
                self.buffers.pop(thread_id, None)

    def bot_sent_ids(self, thread_id):
        with self.store.db() as db:
            return {row[0] for row in db.execute("SELECT message FROM olx_sent WHERE account=? AND thread=? AND speaker='bot'", (self.account, thread_id))}

    def collect(self, thread_id, metadata):
        current_count = count(metadata)
        # Explicit allowlist: unrelated adverts are never read, drafted or exported.
        if str(metadata.get("advert_id")) not in self.catalog.adverts:
            if current_count is not None:
                self.counts[thread_id] = max(self.counts.get(thread_id) or 0, current_count)
            return
        known, previous = thread_id in self.counts, self.counts.get(thread_id)
        if current_count is None:
            self.counts.setdefault(thread_id, None)
            return
        if known and previous is None:
            self.counts[thread_id] = current_count
            return
        if known and current_count <= previous:
            return
        route = self.handoffs.get(self.account, thread_id)
        if route and route["mode"] in ("closed", "blocked", "returning"):
            if current_count is not None:
                self.counts[thread_id] = max(self.counts.get(thread_id) or 0, current_count)
                self.handoffs.advance(self.account, thread_id, current_count)
            return
        if current_count == 0:
            self.counts[thread_id] = 0
            return
        advert, buyer = binding(metadata)
        if route and (advert, buyer) != (route["advert"], route["buyer"]):
            self.handoffs.mode(self.account, thread_id, "blocked")
            self.store.invalidate(self.account, thread_id, "Прив'язка клієнта змінилася")
            say("Переданий діалог %s заблоковано: змінився клієнт або оголошення." % thread_id)
            return
        history = ordered(self.client.messages(thread_id), thread_id, self.catalog.timezone)
        if len(history) != current_count:
            raise AppError("Історія ще не збігається з лічильником OLX. Дочекаюся узгоджених даних.")
        delta = current_count - previous if known else 0
        if known and delta > len(history):
            raise AppError("OLX повернув неповну історію зміненого діалогу; чернетку не створено.")
        if known:
            allowed = {m["_id"] for m in history[-delta:]}
        else:
            # A newly discovered thread can still contain old messages.
            allowed = {m["_id"] for m in history if m["_time"] > self.started_at}
        fresh_messages = [m for m in history if m["_id"] in allowed]
        self.delivery_alerts(thread_id, buyer, advert, fresh_messages)
        if route and route["mode"] != "bot":
            new = [m for m in history if m["_id"] in allowed]
            if route['mode'] == 'human':
                raw = '\n'.join(m.get('text') or '' for m in new if m.get('type') == 'received')
                if wants_dispatch_notice(raw) and not intents(raw).intersection({'cancel', 'complaint'}):
                    context = self.store.context(self.account, thread_id, buyer, advert, self.catalog.data['limits']['memory_days'])
                    if can_watch_shipment(context, raw) and not shipped_waybill(context.get('parcel', {})):
                        context['shipment_notice_invoice'] = context['invoice_id']
                        self.save_dialogue_context(thread_id, buyer, advert, context)
            if route["mode"] == "waiting":
                incoming = [m for m in new if m.get("type") == "received"]
                if incoming:
                    self.handoffs.capture(self.account, thread_id, current_count, history)
                    say("Запит допомоги з діалогу %s надіслано власнику; бот продовжує відповідати." % thread_id)
                else:
                    self.handoffs.advance(self.account, thread_id, current_count)
            else:
                self.handoffs.append(self.account, thread_id, new, current_count)
            self.buffers.pop(thread_id, None)
            self.counts[thread_id] = current_count
            if any(m.get("type") == "received" for m in new):
                self.schedule.activate(thread_id)
            return
        self.store.invalidate(self.account, thread_id)
        old_buffer = self.buffers.get(thread_id)
        if old_buffer and old_buffer["buyer"] == buyer and old_buffer["advert"] == advert:
            allowed.update(old_buffer["source_ids"])
        fresh = candidate(history, allowed, include_delivery_notices=True, transparent_sent=self.bot_sent_ids(thread_id))
        self.counts[thread_id] = current_count
        if self.telegram_config and (route or any(m.get("type") == "received" for m in fresh_messages)):
            self.handoffs.watch(self.account, thread_id, buyer, advert, current_count,
                                self.conversation_title(buyer, advert))
            self.handoffs.title(self.account, thread_id, self.conversation_title(buyer, advert))
            self.handoffs.append(self.account, thread_id, fresh_messages, current_count)
        if not fresh or self.store.handled(self.account, thread_id, fresh["source_ids"][-1]):
            self.buffers.pop(thread_id, None)
            return
        if len(self.buffers) >= 1000 and thread_id not in self.buffers:
            raise AppError("Забагато одночасних діалогів: потрібна ручна перевірка черги.")
        self.buffers[thread_id] = dict(fresh, buyer=buyer, advert=advert,
                                     recent_history=history_excerpt(history, fresh['source_ids']),
                                     due=time.monotonic() + self.catalog.data["polling"]["debounce_seconds"])
        self.schedule.activate(thread_id)

    def poll(self):
        inbox, active = self.schedule.due()
        if inbox:
            adverts = sorted(self.catalog.adverts)
            if not adverts:
                items = []
            elif getattr(self.client, "supports_advert_filter", False) and len(adverts) <= 2:
                items = []
                for advert in adverts:
                    page = self.client.threads(advert_id=advert)
                    if any(str(t.get("advert_id")) != advert for t in page):
                        raise AppError("OLX змінив фільтр оголошення. Обробку зупинено.")
                    items.extend(page)
            else:
                items = self.client.threads()
            threads = [(identifier(t).casefold(), t) for t in items]
            self.schedule.inbox_checked()
            self.schedule.active_checked()
        elif active:
            threads = []
            for thread_id in active:
                try:
                    threads.append((thread_id, self.client.thread(thread_id)))
                except AppError as error:
                    if error.status in (401, 403, 429) or error.status is None:
                        raise
                    self.schedule.deactivate(thread_id)
                    say("Активний діалог %s недоступний; повертаю до звичайної перевірки." % thread_id)
            self.schedule.active_checked(active)
        else:
            return
        for thread_id, metadata in threads:
            if self.stop.is_set():
                break
            try:
                self.collect(thread_id, metadata)
            except AppError as error:
                if error.status in (401, 403, 429):
                    raise
                say("Діалог %s: %s" % (thread_id, error))
                self.schedule.deactivate(thread_id)

    def build(self):
        for thread_id, buffer in list(self.buffers.items()):
            route = self.handoffs.get(self.account, thread_id)
            if route and route["mode"] != "bot":
                self.buffers.pop(thread_id, None)
                continue
            if buffer["due"] > time.monotonic() or self.stop.is_set():
                continue
            context = copy.deepcopy(buffer["input_context"]) if "input_context" in buffer else self.store.context(self.account, thread_id, buffer["buyer"], buffer["advert"], self.catalog.data["limits"]["memory_days"])
            context = self.merge_order_updates(context, self.store.context(self.account, thread_id, buffer["buyer"], buffer["advert"], self.catalog.data["limits"]["memory_days"]))
            mapped_product = self.catalog.adverts.get(buffer["advert"])
            if context.get("_ad_product") != mapped_product and context.get("product") != mapped_product:
                context = {k: v for k, v in context.items() if k in ("height", "weight", "greeted")}
            context["_ad_product"] = mapped_product
            if buffer.get('recent_history'):
                context['recent_history'] = buffer['recent_history']
            if buffer.get('delivery_notices') and not buffer['texts'] and time.time() - context.get('olx_thanked_at', 0) < 600:
                self.buffers.pop(thread_id, None)
                continue
            incoming_feedback = feedback_kind('\n'.join(buffer['texts']), context)
            self.dialogue.merchant = payment_settings(self.store.path.parent)
            status_request = "shipment_status" in intents(normalize("\n".join(buffer["texts"])))
            past = self.store.latest_order(self.account, thread_id, buffer["buyer"], buffer["advert"], self.catalog.data["limits"]["memory_days"]) if status_request and not context.get("invoice_id") else {}
            plan = self.dialogue.plan(buffer["texts"], past or context, buffer["advert"], has_attachments=buffer.get("has_attachments", False), delivery_notice=bool(buffer.get('delivery_notices')))
            if context.get('invoice_issued') and incoming_feedback is None:
                candidates = list(dict.fromkeys(context.get('unclassified_attachment_links', []) + buffer.get('receipt_links', [])))[-3:]
                denied_receipt = bool(re.search(r'не\s+(?:квитанц|квитанці|чек)|фото\s+(?:товару|речі)|випадков|случайно', normalize('\n'.join(buffer['texts']))))
                if plan.context.get('receipt_submitted') and not plan.context.get('unclassified_attachment') and not denied_receipt:
                    plan.context['receipt_links'] = list(dict.fromkeys(context.get('receipt_links', []) + candidates))[-3:]
                    plan.context.pop('unclassified_attachment_links', None)
                elif candidates and not denied_receipt:
                    plan.context['unclassified_attachment_links'] = candidates
            if past:
                plan.context = copy.deepcopy(context)
                plan.on_send = {k: v for k, v in plan.on_send.items() if k in ("greeted", "handoff_now")}
            if plan.guard.get("archive_order") and context.get("invoice_id"):
                self.store.archive_order(self.account, thread_id, buffer["buyer"], buffer["advert"], context)
            payload = dict(buffer)
            payload.pop("due", None)
            payload["texts"] = [str(t)[:3000] for t in buffer["texts"][-20:]]
            payload.update(text=plan.text, context=plan.context, on_send=plan.on_send, guard=plan.guard,
                           input_context=context, notes=plan.notes, catalog=self.base_catalog.signature,
                           product_signature=self.catalog.product_signature(plan.context.get("product"), buffer["advert"]))
            keys = {item["product"] for item in plan.context.get("cart", [])} | set(plan.guard.get("stocks", {})) | set(plan.guard.get("fact_products", []))
            payload["product_signatures"] = {key: self.catalog.product_signature(key, buffer["advert"]) for key in keys}
            limits = self.catalog.data["limits"]
            draft_id = self.store.stage(self.account, self.session, thread_id, payload,
                                        limits["draft_ttl_seconds"], limits["max_pending_drafts"])
            if draft_id is None:
                buffer["due"] = time.monotonic() + 30
                say("Черга відповідей заповнена. Продовжу обробку після надсилання поточних відповідей.")
                continue
            if self.auto_send and draft_id != "handled":
                self.store.decide(draft_id, self.account, self.session, True)
            self.save_dialogue_context(thread_id, buffer["buyer"], buffer["advert"], plan.context)
            self.buffers.pop(thread_id, None)

    def stale(self, draft, note):
        self.store.transition(draft["id"], "approved", "stale", note)
        say("Чернетку %s скасовано: %s" % (draft["id"][:8], note))

    def rebuild(self, draft, note):
        self.stale(draft, note)
        payload, thread_id = draft["payload"], draft["thread"]
        self.buffers[thread_id] = {key: payload[key] for key in ("source_ids", "texts", "fingerprint", "buyer", "advert")}
        self.buffers[thread_id]['delivery_notices'] = payload.get('delivery_notices', [])
        self.buffers[thread_id]['recent_history'] = payload.get('recent_history', [])
        self.buffers[thread_id].update(due=time.monotonic(), input_context=copy.deepcopy(payload.get("input_context", {})),
                                       has_attachments=payload.get("has_attachments", False), receipt_links=payload.get("receipt_links", []))
        with self.store.db() as db:
            db.executemany("DELETE FROM handled WHERE account=? AND thread=? AND message=?",
                           [(self.account, thread_id, mid) for mid in payload["source_ids"]])

    def send_approved(self):
        draft = self.store.next(self.account, self.session, "approved")
        if not draft:
            return
        payload, thread_id = draft["payload"], draft["thread"]
        from payment_exception import GUARD_KEY, valid_draft
        exceptional = GUARD_KEY in payload.get('guard', {})
        if exceptional and not valid_draft(self.inventory, payload):
            self.rebuild(draft, 'Умови винятку змінилися. Повторно перевірю відмови та згоду клієнта.')
            return
        customer_requested_owner = requests_owner('\n'.join(payload.get('texts', [])))
        if payload.get('on_send', {}).get('handoff_now') and not (customer_requested_owner or exceptional):
            self.rebuild(draft, 'Оновлю відповідь відповідно до правила виклику власника на прохання клієнта.')
            return
        if payload.get('on_send', {}).get('handoff_now'):
            payload['on_send'].update(owner_requested=True, owner_request_reason='payment_exception' if exceptional else 'customer')
        route = self.handoffs.get(self.account, thread_id)
        if route and route["mode"] != "bot" or payload["advert"] not in self.catalog.adverts:
            self.stale(draft, "Діалог передано власнику або оголошення не дозволене.")
            return
        if Catalog.load(self.catalog_path).signature != payload["catalog"]:
            self.stale(draft, "Налаштування товарів змінилися. Перезапусти Repliq, щоб їх застосувати.")
            return
        if (payload.get("product_signature") != self.catalog.product_signature(payload["context"].get("product"), payload["advert"]) or
                any(value != self.catalog.product_signature(key, payload["advert"]) for key, value in payload.get("product_signatures", {}).items())):
            self.rebuild(draft, "Товар або прив'язка змінилися. Підготую оновлену відповідь.")
            return
        if str(self.client.identity()["id"]) != self.account:
            raise AppError("Обліковий запис OLX змінився. Потрібен новий запуск.", status=401)
        metadata = self.client.thread(thread_id)
        advert, buyer = binding(metadata)
        if (advert, buyer) != (payload["advert"], payload["buyer"]):
            self.stale(draft, "Змінилася прив’язка клієнта або оголошення.")
            return
        history = ordered(self.client.messages(thread_id), thread_id, self.catalog.timezone)
        current = candidate(history, set(payload["source_ids"]), include_delivery_notices=bool(payload.get('delivery_notices')), transparent_sent=self.bot_sent_ids(thread_id))
        if len(history) != count(metadata) or not current or current["fingerprint"] != payload["fingerprint"]:
            self.stale(draft, "Клієнт або продавець уже дописав у діалог.")
            self.collect(thread_id, metadata)
            return
        guard = payload.get("guard", {})
        if guard.get('advert_description'):
            source = guard['advert_description']
            current_description = self.descriptions.lookup(source['advert']) if self.catalog.adverts.get(source['advert']) == source['product'] else None
            if not current_description or current_description['signature'] != source['signature']:
                self.rebuild(draft, 'Опис оголошення змінився. Оновлю відповідь.')
                return
        if guard.get("payment_settings") and guard["payment_settings"] != payment_signature(payment_settings(self.store.path.parent)):
            self.rebuild(draft, "Реквізити змінилися. Оновлю відповідь.")
            return
        if ((guard.get("shipping_day") and guard["shipping_day"] != str(shop_day(self.catalog.shop))) or
                (guard.get("calendar_day") and guard["calendar_day"] != str(shop_day(self.catalog.shop)))):
            self.rebuild(draft, "Змінився день відправки. Оновлю відповідь.")
            return
        stocks = dict(guard.get("stocks", {}))
        if "product" in guard:
            stocks[guard["product"]] = guard["stock"]
        if stocks:
            try:
                for key, signature in stocks.items():
                    rows = stock_rows(self.inventory, self.catalog.products[key])
                    if stock_signature(rows) != signature:
                        self.rebuild(draft, "Залишок змінився. Підготую оновлену відповідь.")
                        return
            except StockUnavailable:
                self.rebuild(draft, "SQL недоступна; підготую відповідь без обіцянки наявності та передам уточнення власнику.")
                return
        final_metadata = self.client.thread(thread_id)
        if count(final_metadata) != len(history) or binding(final_metadata) != (advert, buyer):
            self.stale(draft, "Діалог змінився під час остаточної перевірки.")
            self.collect(thread_id, final_metadata)
            return
        live = self.store.context(self.account, thread_id, buyer, advert, self.catalog.data["limits"]["memory_days"])
        expected = payload["context"]
        if (expected.get("invoice_id") and expected.get("invoice_id") == live.get("invoice_id") and
                (bool(expected.get("payment_verified")), expected.get("verified_invoice_key")) !=
                (bool(live.get("payment_verified")), live.get("verified_invoice_key"))):
            self.rebuild(draft, "Позначка оплати змінилася. Оновлю відповідь клієнту.")
            return
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            route = self.handoffs.get(self.account, thread_id)
            if route and route["mode"] != "bot":
                self.stale(draft, "Власник змінив режим керування діалогом.")
                return
            if self.stop.is_set() or not self.store.transition(draft["id"], "approved", "sending"):
                return
            notify_owner = bool(payload.get("on_send", {}).get("handoff_now") and (customer_requested_owner or exceptional) and not (route and route.get('attention')))
            if notify_owner:
                title = self.conversation_title(buyer, advert)
                # Claim and help routing are atomic with an owner's takeover.
                self.handoffs.waiting(self.account, thread_id, buyer, advert, len(history), title)
                if not self.telegram_config:
                    say("Для передачі діалогу %s заповни private/telegram.json. Бот продовжує консультацію." % thread_id)
        # Commit sending BEFORE the non-idempotent POST. Never automatically retry it.
        try:
            sent = self.client.send(thread_id, payload["text"])
        except Exception as error:
            self.store.transition(draft["id"], "sending", "uncertain", "Перевір повідомлення в OLX. Автоповтор вимкнено.")
            say("Невідомий результат надсилання в діалог %s. Перевір OLX; повтору не буде." % thread_id)
            self.handoffs.waiting(self.account, thread_id, buyer, advert, len(history), "Перевірити діалог · " + buyer)
            self.handoffs.mode(self.account, thread_id, "blocked")
            self.handoffs.control(self.account, thread_id, "⚠️ Результат надсилання невідомий. Перевір останнє повідомлення в OLX перед продовженням.", "uncertain:" + draft["id"])
            if isinstance(error, AppError) and error.status in (401, 403, 429):
                raise
            return
        self.store.transition(draft["id"], "sending", "sent")
        self.handoffs.mark_sent(self.account, thread_id, sent, "bot")
        self.store.set_meta('manual-pending:%s:%s' % (self.account, thread_id), {})
        context = dict(payload["context"], **payload.get("on_send", {}))
        if payload.get('on_send', {}).get('invoice_issued') and context.get('checkout_method') == 'cod' and self.dialogue.merchant.get('_monobank'):
            key = invoice_key(snapshot(self.catalog, context))
            if context.get('bank_payment', {}).get('invoice_key') != key:
                context['bank_payment'] = bank_marker(self.dialogue.merchant['_monobank'], key)
        if self.fulfillment.queue and payload.get("on_send", {}).get("invoice_issued"):
            context["fulfillment_opt_in"] = True
        context = self.save_dialogue_context(thread_id, buyer, advert, context)
        self.monobank.watch_invoice(self.account, thread_id, context, self.catalog)
        old_invoice = payload.get("input_context", {}).get("invoice_id")
        if self.fulfillment.queue and old_invoice and not context.get("invoice_id") and not payload.get("guard", {}).get("archive_order"):
            self.fulfillment.queue.cancel(self.account, thread_id, old_invoice)
            self.monobank.cancel_invoice(self.account, thread_id, old_invoice)
        self.fulfillment.sync(self.account, thread_id, buyer, advert, context, self.catalog)
        if notify_owner:
            outgoing = dict(sent, _id=identifier(sent), type="sent", text=payload["text"])
            self.handoffs.capture(self.account, thread_id, len(history), history + [outgoing], latest=payload["source_ids"])
            route = self.handoffs.get(self.account, thread_id)
            owner = admin_owner(self.telegram_config)
            if owner and route and route.get("topic_id") and route.get('attention'):
                group = str(route["group_id"])
                keyboard = {"inline_keyboard": [[{"text": "Допомогти клієнту", "url": "https://t.me/c/%s/%s" % (group[4:], route["topic_id"])}]]} if group.startswith("-100") else None
                self.registry.tell(owner, "needs-owner:" + draft["id"], "🙋 Потрібна твоя допомога.\n" + route["title"], keyboard)
        elif self.telegram_config:
            outgoing = dict(sent, _id=identifier(sent), type="sent", text=payload["text"])
            self.handoffs.append(self.account, thread_id, [outgoing], len(history) + 1)
            if payload.get('on_send', {}).get('handoff_now'):
                self.handoffs.problem(self.account, thread_id, history, payload['source_ids'])
        if self.orders.sync(self.account, thread_id, context, self.catalog) is not None:
            route = self.handoffs.get(self.account, thread_id)
            if route:
                self.handoffs.control(self.account, thread_id, status_text(route["mode"]), "order-card:" + draft["id"])
        self.schedule.activate(thread_id)
        say("✓ Відповідь надіслано · діалог %s." % thread_id)

    def parcel_can_create(self, row):
        data = row["data"]
        route = self.handoffs.get(data["account"], data["thread"])
        if not route or route["mode"] not in ("bot", "human", "returning"):
            return False
        context = self.store.order_context(data["account"], data["thread"], data["buyer"], data["advert"], data["invoice_id"], self.catalog.data["limits"]["memory_days"])
        latest = parcel_payload(self.catalog, context, data["account"], data["thread"], data["buyer"], data["advert"])
        return latest is not None and parcel_digest(latest) == row["content_key"]

    def parcel_updates(self):
        if not self.fulfillment.queue:
            return
        for row in self.fulfillment.queue.updates(self.account, max(0, self.fulfillment_cursor - .1)):
            self.fulfillment_cursor = max(self.fulfillment_cursor, row["updated"])
            data = row["data"]
            context = self.invoice_context(row["thread"], data["buyer"], data["advert"], row["invoice_id"])
            current = parcel_payload(self.catalog, context, self.account, row["thread"], data["buyer"], data["advert"])
            if context.get("invoice_id") != row["invoice_id"]:
                continue
            parcel = {"id": row["id"], "ttn": row["ttn"], "status": row["nova_status"], "profile": row["nova_profile"],
                      "ledgerly_status": row["ledgerly_status"], "error": row["error"]}
            # Only an explicit owner decision can request another receipt.
            if (row.get('payment_source') == 'owner' and row.get('payment_manual_lock') and
                    not row['paid'] and row.get('paid_by') and row.get('payment_review_revision') and
                    row.get('payment_review_receipts') == row.get('receipt_links') and
                    row['ledgerly_status'] not in ('shipped', 'cancelled')):
                parcel['payment_review_revision'] = row['payment_review_revision']
            if data.get('payment_policy') == 'carrier_delivery':
                parcel.update(delivery_state=row['delivery_state'], delivery_cost_cents=row['delivery_cost_cents'], delivery_reason=row['delivery_reason'])
            payment_matches = current and current["invoice_key"] == row["invoice_key"]
            payment_changed = payment_matches and bool(context.get("payment_verified")) != bool(row["paid"])
            if context.get("parcel") == parcel and not payment_changed:
                if parcel.get('payment_review_revision') or (shipped_waybill(parcel) and context.get('shipment_notice_invoice') == row['invoice_id']):
                    self.customer_updates.schedule(self.account, row['thread'], data['buyer'], data['advert'], context, self.catalog)
                continue
            context["parcel"] = parcel
            if payment_matches:
                context.update(payment_verified=bool(row["paid"]), payment_verified_by=row["paid_by"], verified_invoice_key=row["invoice_key"] if row["paid"] else None)
            live = self.store.context(self.account, row["thread"], data["buyer"], data["advert"], self.catalog.data["limits"]["memory_days"])
            if live.get("invoice_id") == row["invoice_id"]:
                self.store.save_context(self.account, row["thread"], data["buyer"], data["advert"], context)
                self.orders.sync(self.account, row["thread"], context, self.catalog)
            else:
                self.store.archive_order(self.account, row["thread"], data["buyer"], data["advert"], context)
            self.customer_updates.schedule(self.account, row["thread"], data["buyer"], data["advert"], context, self.catalog)
            route = self.handoffs.get(self.account, row["thread"])
            if route:
                self.handoffs.control(self.account, row["thread"], status_text(route["mode"]), "parcel:" + row["id"] + ":" + str(row["updated"]))

    def send_human_reply(self):
        item = self.handoffs.next_reply(self.account)
        if not item:
            return
        route = self.handoffs.get(self.account, item["thread"])
        config = self.telegram_config
        allowed = route and (route["mode"] == "human" or item['kind'] == 'reply' and route['mode'] == 'returning' or item["kind"] == "history" and route["mode"] in ("paused", "bot", "closed", "blocked") or
                             item["kind"] in ("return_bot", "switch_bot") and route["mode"] == "returning")
        valid = (config and route and allowed and
                 item["owner_id"] in config["owner_ids"] and item["group_id"] == config["group_id"] == route["group_id"] and
                 item["topic_id"] == route["topic_id"] and route["advert"] in self.catalog.adverts)
        if not valid or time.time() - item["created"] > 900:
            self.handoffs.finish_reply(item["event_key"], "stale", "Неактивна прив'язка, власник або час відповіді")
            if route and item['kind'] == 'reply' and item['owner_id'] in (config or {}).get('owner_ids', []):
                self.handoffs.notice(self.account, item['thread'], 'Повідомлення не надіслано: минув час очікування або змінився стан діалогу. За потреби повтори його в актуальній темі.', 'stale-reply:' + item['event_key'])
            return
        if str(self.client.identity()["id"]) != self.account:
            raise AppError("Обліковий запис OLX змінився; відповіді Telegram зупинені.", status=401)
        metadata = self.client.thread(item["thread"])
        if binding(metadata) != (route["advert"], route["buyer"]):
            self.handoffs.mode(self.account, item["thread"], "blocked")
            self.handoffs.finish_reply(item["event_key"], "stale", "Змінився покупець")
            return
        if item["kind"] in ("return_bot", "switch_bot"):
            current_count = count(metadata)
            if current_count is None:
                raise AppError("OLX ще не передав лічильник діалогу. Повторю повернення боту.")
            resume_history = ordered(self.client.messages(item["thread"]), item["thread"], self.catalog.timezone) if item["kind"] == "switch_bot" else []
            if resume_history and len(resume_history) != current_count:
                return
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                current_route = self.handoffs.get(self.account, item["thread"])
                if current_route["mode"] != "returning":
                    self.handoffs.finish_reply(item["event_key"], "cancelled")
                    return
                if item['kind'] == 'switch_bot':
                    self.merge_manual_choices(route, resume_history)
                self.handoffs.advance(self.account, item["thread"], current_count)
                self.handoffs.mode(self.account, item["thread"], "bot")
                if item["kind"] == "return_bot":
                    previous = self.store.context(self.account, item["thread"], route["buyer"], route["advert"], self.catalog.data["limits"]["memory_days"])
                    self.store.archive_order(self.account, item["thread"], route["buyer"], route["advert"], previous)
                    db.execute("DELETE FROM chats WHERE account=? AND thread=?", (self.account, item["thread"]))
                    self.orders.clear(self.account, item["thread"])
                self.store.invalidate(self.account, item["thread"], "Угоду завершено, починаємо новий діалог")
                self.handoffs.finish_reply(item["event_key"], "sent")
                self.handoffs.control(self.account, item["thread"], status_text("bot"), "returned:" + item["event_key"])
            self.counts[item["thread"]] = current_count
            self.buffers.pop(item["thread"], None)
            self.schedule.deactivate(item["thread"])
            if item["kind"] == "switch_bot":
                self.resume_candidate(route, resume_history, metadata)
            return
        if item["kind"] == "history":
            history = ordered(self.client.messages(item["thread"]), item["thread"], self.catalog.timezone)
            self.handoffs.history(self.account, item["thread"], history, "manual-history:" + item["event_key"])
            self.handoffs.finish_reply(item["event_key"], "sent")
            return
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT mode FROM handoffs WHERE account=? AND thread=?", (self.account, item["thread"])).fetchone()
            claimed = active and active[0] in ('human','returning') and db.execute(
                "UPDATE bridge_replies SET status='sending' WHERE event_key=? AND status='queued' AND created>?",
                (item["event_key"], time.time() - 900)).rowcount
        if not claimed or self.stop.is_set():
            return
        try:
            result = self.client.send(item["thread"], item["text"])
        except Exception as error:
            self.handoffs.finish_reply(item["event_key"], "uncertain", "Перевір OLX; автоматичного повтору немає", "sending")
            self.handoffs.mode(self.account, item['thread'], 'blocked')
            self.handoffs.notice(self.account, item["thread"], "Невідомий результат надсилання. Перевір OLX; повтору не буде.", "receipt:" + item["event_key"])
            self.handoffs.control(self.account, item['thread'], status_text('blocked'), 'uncertain-reply:' + item['event_key'])
            if isinstance(error, AppError) and error.status in (401, 403, 429):
                raise
            return
        self.handoffs.finish_reply(item["event_key"], "sent", expected="sending")
        self.handoffs.mark_sent(self.account, item["thread"], result, "owner", seen=True)
        self.store.set_meta('manual-pending:%s:%s' % (self.account, item['thread']), {})
        with self.store.db() as db:
            db.execute('UPDATE handoffs SET attention=0 WHERE account=? AND thread=?', (self.account,item['thread']))
            self.handoffs.enqueue(db, "receipt:" + item["event_key"], self.account, item["thread"], "owner", "✓ Надіслано в OLX.")
        self.handoffs.control(self.account, item['thread'], status_text('human'), 'sent:' + item['event_key'])
        self.schedule.activate(item["thread"])

    def send_customer_update(self):
        self.customer_updates.sync_cards(self.account, self.store, self.catalog)
        item = self.customer_updates.next(self.account)
        if not item or self.stop.is_set() or item["thread"] in self.buffers:
            return
        context = self.invoice_context(item["thread"], item["buyer"], item["advert"], item["invoice_id"])
        order = snapshot(self.catalog, context)
        parcel = order.get("parcel", {})
        from carrier_payment import direct_delivery, enabled as carrier_enabled, ready, waybill_message
        review_request = item['kind'] == 'payment_review'
        shipped_update = item['kind'] == 'shipped'
        payment_required = not (review_request or shipped_update) and (item['kind'] != 'waybill' or not direct_delivery(order))
        if (order["id"] != item["invoice_id"] or invoice_key(order) != item["invoice_key"] or
                order["confirmation"] != confirmation_key(order) or
                parcel.get("status") in ("needs_review", "invalid", "uncertain", "cancelled")):
            self.customer_updates.finish(item["id"], "stale", "order_changed")
            return
        if review_request and (context.get('payment_verified') or parcel.get('payment_review_revision') != item['revision']):
            self.customer_updates.finish(item['id'], 'stale', 'payment_review_resolved')
            return
        if payment_required and (not context.get('payment_verified') or context.get('verified_invoice_key') != item['invoice_key']):
            self.customer_updates.finish(item['id'], 'stale', 'payment_wait')
            return
        if item["kind"] == "dispatch" and (context.get("dispatch_announced_key") == item["invoice_key"] or parcel.get("ledgerly_status") == "shipped"):
            self.customer_updates.finish(item["id"], "sent", "already_announced")
            return
        if item["kind"] == "waybill" and (parcel.get("status") != "created" or parcel.get("ttn") != item["ttn"]):
            self.customer_updates.finish(item["id"], "stale", "waybill_changed")
            return
        if shipped_update and (shipped_waybill(parcel) != item['ttn'] or context.get('shipment_notice_invoice') != item['invoice_id']):
            self.customer_updates.finish(item['id'], 'stale', 'shipment_changed')
            return
        if shipped_update and context.get('shipment_notice_announced') == item['ttn']:
            self.customer_updates.finish(item['id'], 'sent', 'announced_in_reply')
            return
        if item['kind'] == 'waybill' and shipped_waybill(parcel) and context.get('shipment_notice_invoice') == order['id']:
            self.customer_updates.finish(item['id'], 'stale', 'included_in_shipping_notice')
            return
        if str(self.client.identity()["id"]) != self.account:
            raise AppError("Акаунт OLX змінився; повідомлення про відправку зупинені.", status=401)
        metadata = self.client.thread(item["thread"])
        if count(metadata) is None:
            return
        if binding(metadata) != (item["advert"], item["buyer"]):
            self.customer_updates.finish(item["id"], "stale", "recipient_changed")
            return
        if count(metadata) != self.counts.get(item["thread"]):
            self.collect(item["thread"], metadata)
            return
        if review_request:
            text = 'На жаль, оплату не вдалося підтвердити. Надішліть, будь ласка, квитанцію ще раз — фото або скріншот у цей чат.'
        elif shipped_update:
            history = ordered(self.client.messages(item['thread']), item['thread'], self.catalog.timezone)
            if len(history) != count(metadata):
                return
            reference = re.escape(item['ttn']) + '|' + re.escape(item['invoice_id'])
            for message in history:
                body = str(message.get('text') or '')
                if (message.get('type') == 'sent' and re.search(r'(?<!\w)(?:' + reference + r')(?!\w)', body, re.I) and
                        re.search(r'\b(?:відправлено|відправили|відправив|відправила|отправлено|отправили|отправил|отправила)\b', body, re.I) and
                        not re.search(r'\b(?:не|ще)\s+(?:відправл|відправи|отправл|отправи)', body, re.I)):
                    self.customer_updates.finish(item['id'], 'sent', 'already_in_olx')
                    return
            text = 'Посилку відправлено. Ваша ТТН — ' + item['ttn'] + '.\nЗа нею можна відстежити посилку в Новій пошті.'
        elif item["kind"] == "waybill":
            history = ordered(self.client.messages(item["thread"]), item["thread"], self.catalog.timezone)
            if any(m.get("type") == "sent" and re.search(r"(?<!\d)" + re.escape(item["ttn"]) + r"(?!\d)", str(m.get("text", ""))) for m in history):
                self.customer_updates.finish(item["id"], "sent", "already_in_olx")
                return
            text = (waybill_message(order['id'], item['ttn']) if direct_delivery(order) and carrier_enabled(self.catalog.shop) else
                    "Ось ваша ТТН: " + item["ttn"] + "\nВаше замовлення №" + order['id'] + "\nНакладну підготовлено для відправлення.")
        elif item['kind'] == 'delivery_paid':
            intro = 'Дякую, оплату доставки підтверджено! ' if carrier_enabled(self.catalog.shop) else 'Дякую! '
            suffix = ('Посилку вже відправлено.' if context.get('shipment_notice_invoice') != order['id'] else '') if shipped_waybill(parcel) else shipping_reply(self.catalog.shop)
            text = (intro + suffix).strip() + '\nВаше замовлення №' + order['id']
        else:
            text = "Дякую! " + shipping_reply(self.catalog.shop)
        active = self.store.context(self.account, item["thread"], item["buyer"], item["advert"], self.catalog.data["limits"]["memory_days"])
        if active.get("invoice_id") != item["invoice_id"]:
            goods = ", ".join(dict.fromkeys(i.get("name", "товар") for i in order["items"]))[:240]
            text = "Щодо вашого замовлення (%s):\n" % goods + text
        latest = self.client.thread(item["thread"])
        route = self.handoffs.get(self.account, item["thread"])
        if (not route or route["mode"] not in ("bot", "human") or binding(latest) != (item["advert"], item["buyer"]) or count(latest) != count(metadata)):
            return
        current = self.invoice_context(item["thread"], item["buyer"], item["advert"], item["invoice_id"])
        current_order = snapshot(self.catalog, current)
        if review_request and (current.get('payment_verified') or current.get('parcel', {}).get('payment_review_revision') != item['revision']):
            self.customer_updates.finish(item['id'], 'stale', 'payment_review_resolved')
            return
        if ((payment_required and (not current.get("payment_verified") or current.get("verified_invoice_key") != item["invoice_key"])) or
                invoice_key(current_order) != item["invoice_key"] or current_order["confirmation"] != confirmation_key(current_order)):
            return
        if shipped_update and (current.get('shipment_notice_invoice') != item['invoice_id'] or
                               shipped_waybill(current.get('parcel', {})) != item['ttn'] or
                               current.get('shipment_notice_announced') == item['ttn']):
            self.customer_updates.finish(item['id'], 'stale', 'shipment_changed')
            return
        if self.fulfillment.queue and current.get("parcel", {}).get("id"):
            row = self.fulfillment.queue.get(current["parcel"]["id"])
            if not row or row['invoice_key'] != item['invoice_key'] or row["nova_status"] in ("needs_review", "invalid", "uncertain", "cancelled"):
                self.customer_updates.finish(item['id'], 'stale', 'order_changed')
                return
            if payment_required and not ready(row):
                self.customer_updates.finish(item['id'], 'stale', 'payment_wait')
                return
            if shipped_update and (row['ledgerly_status'] != 'shipped' or row['nova_status'] != 'created' or row['ttn'] != item['ttn']):
                self.customer_updates.finish(item['id'], 'stale', 'shipment_changed')
                return
            if review_request and (row['paid'] or row.get('payment_source') != 'owner' or
                                   not row.get('paid_by') or not row.get('payment_manual_lock') or
                                   row.get('payment_review_revision') != item['revision'] or
                                   row.get('payment_review_receipts') != row.get('receipt_links') or
                                   row['ledgerly_status'] in ('shipped', 'cancelled')):
                self.customer_updates.finish(item['id'], 'stale', 'payment_review_resolved')
                return
        elif review_request or shipped_update:
            self.customer_updates.finish(item['id'], 'stale', 'shipment_unavailable' if shipped_update else 'payment_review_unavailable')
            return
        if not self.customer_updates.claim(item["id"]):
            return
        try:
            sent = self.client.send(item["thread"], text)
        except Exception as error:
            self.customer_updates.finish(item["id"], "uncertain", "send_result_unknown")
            self.handoffs.notice(self.account, item["thread"], "Перевір в OLX останнє повідомлення клієнту: результат надсилання невідомий. Автоповтор вимкнений.", "customer-update:" + item["id"])
            if isinstance(error, AppError) and error.status in (401, 403, 429):
                raise
            return
        self.customer_updates.finish(item["id"], "sent")
        self.handoffs.mark_sent(self.account, item["thread"], sent, "bot")
        outgoing = dict(sent, _id=identifier(sent), type="sent", text=text)
        self.handoffs.append(self.account, item["thread"], [outgoing], count(metadata) + 1)
        self.counts[item["thread"]] = count(metadata) + 1
        self.schedule.activate(item["thread"])

    def process_cycle(self):
        # A reply that is already ready must not wait for polling every other chat.
        # send_approved still performs all current identity, message and stock checks.
        self.build()
        self.send_approved()
        self.send_human_reply()
        self.poll()
        self.build()
        self.send_approved()
        self.parcel_updates()
        self.send_customer_update()
        if not self.buffers:
            self.refresh_buyer_names()

    def run(self):
        guard, guard_held = None, False
        try:
            # Shares the old prototype's lock: old and new workers cannot coexist.
            guard = private_lock(self.store.path.parent / "responder-state.lock", blocking=False)
            guard.__enter__()
            guard_held = True
            self.initialize()
            self.fulfillment.start()
            self.monobank.start()
            while not self.stop.is_set():
                now = time.monotonic()
                if now - self.last_heartbeat >= 10:
                    self.heartbeat()
                if now - self.last_cleanup >= 60:
                    purge = self.last_purge is None or now - self.last_purge >= 3600
                    self.store.cleanup(self.catalog.data["limits"]["memory_days"], purge=purge)
                    self.handoffs.cleanup(self.catalog.data["limits"]["memory_days"], purge=purge)
                    if purge:
                        self.customer_updates.cleanup(self.catalog.data["limits"]["memory_days"])
                        self.registry.cleanup(self.catalog.data["limits"]["memory_days"])
                        self.orders.cleanup(self.catalog.data["limits"]["memory_days"])
                        self.last_purge = now
                    self.last_cleanup = now
                # Local Telegram edits remain available during OLX backoff.
                process_admin(self)
                if now >= self.retry_at:
                    try:
                        if now >= self.next_product_sync:
                            self.refresh_products()
                        self.resume_expired()
                        self.process_cycle()
                        self.failures = 0
                    except AppError as error:
                        if error.status in (401, 403):
                            raise
                        self.failures += 1
                        delay = min(300, (60 if error.status == 429 else 5) * 2 ** min(self.failures - 1, 5))
                        self.retry_at = time.monotonic() + delay
                        say("%s Наступна спроба через %s с." % (error, delay))
                delay = 1.0
                if time.monotonic() >= self.retry_at:
                    delay = min(delay, self.schedule.delay())
                    if self.buffers:
                        delay = min(delay, max(0.0, min(b["due"] for b in self.buffers.values()) - time.monotonic()))
                self.stop.wait(max(0.05, delay))
        except BlockingIOError:
            self.failure = "Інший автовідповідач уже працює в цій папці."
        except AppError as error:
            self.failure = str(error)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            self.failure = "Помилка локальних даних Repliq. Перевір налаштування й вільне місце на диску."
        except Exception as error:
            self.failure = "Неочікувана помилка %s; надсилання зупинено." % type(error).__name__
        finally:
            self.stop.set()
            self.ready.set()
            try:
                self.inventory.close()
                if self.fulfillment.thread:
                    self.fulfillment.thread.join(timeout=3)
                if self.monobank.thread:
                    self.monobank.thread.join(timeout=9)
                if self.telegram_thread:
                    self.telegram_thread.join(timeout=13)
                if self.account:
                    self.heartbeat("stopped")
            finally:
                self.store.close_current()
                if guard_held:
                    guard.__exit__(None, None, None)
            if self.failure:
                say(self.failure)


def prepare_input():
    if not sys.stdin.isatty():
        raise AppError("Підтвердження працює лише в інтерактивному терміналі.")
    import termios
    termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)


def approvals(store, stop, fixed_session=None):
    say("Черга відповідей: «так» — надіслати; «ні» або Enter — пропустити; «текст» — змінити; «вихід» — закрити.")
    while not stop.is_set():
        live = store.meta("worker")
        if live.get("auto_send"):
            raise AppError("Repliq відповідає автоматично. Діалогами керуй кнопками в Telegram.")
        if not live or live.get("status") != "running" or time.time() - live.get("heartbeat", 0) > 120:
            if fixed_session:
                stop.wait(0.5)
                continue
            raise AppError("Активного Repliq не знайдено. Спочатку запусти автовідповідач.")
        if fixed_session and live["session"] != fixed_session:
            raise AppError("Робочий сеанс змінився. Відкрий чергу повторно.")
        draft = store.next(live["account"], live["session"])
        if not draft:
            stop.wait(1)
            continue
        p = draft["payload"]
        say("\n%s\nЧернетка %s · діалог %s · клієнт %s · оголошення %s\nКлієнт:\n%s\n\nВідповідь:\n%s\n%s" % (
            "─" * 55, draft["id"][:8], draft["thread"], p["buyer"], p["advert"],
            "\n".join("  │ " + clean(t)[:2000] for t in p["texts"]), p["text"],
            "\n".join("Для тебе: " + n for n in p.get("notes", []))))
        prepare_input()
        answer = input("Надіслати? [так / ні / текст / вихід]: ").strip().casefold()
        if answer in ("вихід", "exit", "q"):
            return
        replacement = None
        if answer in ("текст", "редагувати"):
            replacement = input("Нова відповідь (одним рядком; порожньо — скасувати): ").strip()
            if not replacement:
                continue
            say("Нова відповідь:\n" + replacement)
            prepare_input()
            answer = input("Надіслати саме цей текст? [так / ні]: ").strip().casefold()
        if answer not in ("так", "ні", "", "yes", "no", "y", "n"):
            say("Введи «так», «ні», «текст» або «вихід».")
            continue
        success = store.decide(draft["id"], live["account"], live["session"], answer in ("так", "yes", "y"), replacement)
        say("Рішення прийнято." if success else "Ця чернетка вже змінилася або застаріла. Потрібне нове підтвердження.")


def demo(catalog):
    dialogue, context = Dialogue(catalog, DemoInventory()), {}
    say("ДЕМО · вигадані залишки; нічого не надсилається. Напиши повідомлення покупця або «вихід».")
    while True:
        text = input("Клієнт: ")
        if text.strip().casefold() in ("вихід", "exit", "q"):
            return
        plan = dialogue.plan([text], context)
        say("Чернетка: " + plan.text)
        for note in plan.notes:
            say("Для тебе: " + note)
        context = plan.context
        if input("Підтвердити демовідповідь? [так / ні]: ").strip().casefold() == "так":
            context.update(plan.on_send)
            say("Демо: відповідь підтверджено.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Repliq: автоматичні консультації OLX і діалоги в Telegram.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--worker", action="store_true", help="Фонова автоматична перевірка й відповіді без консолі підтверджень")
    mode.add_argument("--approve", action="store_true", help="Відкрити чергу вже запущеного процесу")
    mode.add_argument("--manual", action="store_true", help="Тестовий режим: підтвердження відповідей у консолі")
    mode.add_argument("--demo", action="store_true", help="Діалог із вигаданими залишками, без OLX і SQL")
    mode.add_argument("--status", action="store_true", help="Стан процесу та надсилання з невідомим результатом")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "private")
    parser.add_argument("--catalog", type=Path, default=ROOT / "shop.json")
    args = parser.parse_args()
    stop, thread, store = threading.Event(), None, None
    try:
        if args.demo:
            demo(Catalog.load(args.catalog))
            return 0
        if args.approve and not (args.data_dir / "conversations.sqlite3").exists():
            raise AppError("Черга ще не створена. Спочатку запусти автовідповідач.")
        store = Store(args.data_dir / "conversations.sqlite3")
        if args.status:
            account = store.meta("worker").get("account", "")
            handoffs = Handoffs(store)
            with store.db() as db:
                bridge_review = [dict(r) for r in db.execute("SELECT event_key,thread,status,note FROM bridge_replies "
                                                           "WHERE status IN ('sending','uncertain') LIMIT 30")]
            say(json.dumps({"worker": worker_status(store), "telegram": store.meta("telegram-status:" + account),
                            "telegram_last_error": store.meta("telegram-last-error:" + account), "needs_review": store.report(),
                            "handoffs": handoffs.all(account), "telegram_needs_review": bridge_review}, ensure_ascii=False, indent=2))
            return 0
        if args.approve:
            approvals(store, stop)
            return 0
        catalog = Catalog.load(args.catalog)
        app = Repliq(data_dir=args.data_dir)
        if not read_json(app.tokens_path).get("access_token"):
            raise AppError("Спочатку підключи OLX у Repliq. Збережені ключі повторно вводити не потрібно.")
        inventory = Inventory(database_url(args.data_dir))
        worker = Worker(OlxClient(app), store, catalog, args.catalog, inventory, stop, auto_send=not args.manual)
        if not args.manual:
            signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
            signal.signal(signal.SIGHUP, lambda signum, frame: stop.set())
            worker.run()
        else:
            if not sys.stdin.isatty():
                raise AppError("Для фонової роботи використай --worker, для підтверджень — --approve у терміналі.")
            thread = threading.Thread(target=worker.run, name="repliq-network", daemon=True)
            thread.start()
            while not worker.ready.wait(0.25):
                pass
            if not stop.is_set():
                approvals(store, stop, worker.session)
        return 1 if worker.failure else 0
    except (KeyboardInterrupt, EOFError):
        return 0
    except (AppError, sqlite3.Error, OSError, ValueError) as error:
        say(str(error) if isinstance(error, AppError) else "Помилка локальних файлів. Перевір налаштування та доступ до private/.")
        return 1
    finally:
        stop.set()
        if thread:
            thread.join(timeout=30)
        if store:
            store.close_current()


if __name__ == "__main__":
    raise SystemExit(main())
