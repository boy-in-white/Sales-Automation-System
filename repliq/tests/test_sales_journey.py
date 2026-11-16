"""The actual automatic worker across checkout, payment proof and Telegram ownership."""

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from catalog import Catalog
from chat_store import Store
from sales_service import Worker
from telegram_bridge import Bridge
from test_consultation_flow import Stock, TEST_CARD, ROOT
from test_sales import FakeOlx


class ConversationOlx(FakeOlx):
    def send(self, thread_id, text):
        self.sent.append((thread_id, text))
        message = {"uuid": "out-%s" % len(self.history), "id": len(self.history) + 1, "thread_uuid": thread_id,
                   "type": "sent", "text": text, "created_at": datetime.now(timezone.utc).isoformat()}
        self.history.append(message)
        return dict(message)


class Api:
    def __init__(self):
        self.calls = []

    def call(self, method, fields):
        self.calls.append((method, fields))
        return {"message_id": 501, "message_thread_id": 10}


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        data = json.loads((ROOT / "shop.json").read_text())
        for product, ad, price in zip(data["products"], (101, 102), (800, 500)):
            product.update(advert_ids=[ad], ledgerly_product_id=product["key"], price_uah=price)
        self.path = self.root / "shop.json"
        self.path.write_text(json.dumps(data))
        (self.root / "payments.json").write_text(json.dumps({"enabled": True, "kind": "card", "account": TEST_CARD}))
        self.store = Store(self.root / "state.sqlite3")
        self.addCleanup(self.store.close_current)
        self.client, self.stock = ConversationOlx(), Stock()
        self.worker = Worker(self.client, self.store, Catalog(data), self.path, self.stock, threading.Event(), auto_send=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.worker.initialize()
        self.config = {"bot_token": "test", "group_id": -100123, "owner_ids": [7]}
        self.worker.telegram_config = self.config
        self.api = Api()
        self.bridge = Bridge(self.worker.handoffs, self.config, "account", threading.Event(), api=self.api)

    def owner_topic(self):
        """An explicitly opened owner topic, not automatic forwarding of normal chats."""
        with self.store.db() as db:
            db.execute("UPDATE handoffs SET group_id=-100123,topic_id=10,topic_state='ready',control_message_id=501 WHERE account='account' AND thread='t'")
            db.execute("INSERT OR REPLACE INTO bridge_refs VALUES(-100123,501,'account','t','control')")
        self.worker.handoffs.control('account','t','auto','explicit-owner-topic')
        self.bridge.next_send=0
        self.bridge.deliver()

    def prepare(self, text, attachment=False):
        self.client.incoming(text)
        if attachment:
            self.client.history[-1]["attachments"] = [{"url": "https://example.test/receipt.jpg"}]
        self.worker.collect("t", self.client.thread("t"))
        if "t" in self.worker.buffers:
            self.worker.buffers["t"]["due"] = 0
        self.worker.build()

    def turn(self, text, attachment=False):
        self.prepare(text, attachment)
        self.worker.send_approved()
        return self.client.sent[-1][1] if self.client.sent else ""

    def test_real_worker_waits_for_details_and_receipt_before_owner_takeover(self):
        self.turn('Хочу замовити чорні M')
        self.assertEqual(self.worker.handoffs.get('account','t')['mode'],'bot')
        self.assertFalse(self.bridge.create_topic())
        self.assertIn('100 грн',self.turn('Післяплата'))
        self.turn('Петренко Іван Іванович\n0671234567\nЛьвів, Львівська область\n12')
        answer=self.turn('так')
        self.assertRegex(answer,'Відправка|Постараємося відправити')
        self.turn('Квитанція',attachment=True)
        context=self.store.context('account','t','buyer','101',30)
        self.assertTrue(context['receipt_submitted'])
        self.assertFalse(context.get('payment_verified'))
        self.assertEqual(self.worker.handoffs.get('account','t')['mode'],'bot')
        before=len(self.client.sent)
        self.turn('Яка сума доставки?')
        self.assertGreater(len(self.client.sent),before)

    def test_watch_topic_is_created_on_first_message_without_turning_off_bot(self):
        self.turn('Привіт')
        self.assertFalse(self.bridge.create_topic())
        route=self.worker.handoffs.get('account','t')
        self.assertEqual(route['mode'],'bot')
        self.assertIsNone(route['topic_id'])
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bridge_outbox WHERE kind IN ('customer','seller') AND status='pending'").fetchone()[0],0)
        self.assertFalse(any(method=='sendMessage' for method,_ in self.api.calls))

    def test_cart_rebuild_after_stock_change_does_not_add_the_same_item_twice(self):
        self.turn("Хочу штани M чорні і футболку L білу")
        self.prepare("Додай ще футболку L білу")
        self.stock.values[("gallery-tee", "S", "GRAY")] = 1
        sent_before = len(self.client.sent)
        self.worker.send_approved()
        self.assertEqual(len(self.client.sent), sent_before)
        self.worker.build()
        self.worker.send_approved()
        context = self.store.context("account", "t", "buyer", "101", 30)
        self.assertEqual(context["cart"][1]["quantity"], 2)
        self.assertIn("1800", self.client.sent[-1][1])

    def test_second_product_fact_change_invalidates_cart_reply(self):
        self.prepare("Хочу штани M чорні і футболку L білу")
        self.worker.catalog.products["gallery-tee"]["price_uah"] = 600
        self.worker.send_approved()
        self.assertEqual(self.client.sent, [])
        self.worker.build()
        self.worker.send_approved()
        self.assertIn("1400", self.client.sent[-1][1])

    def test_changed_payment_settings_are_rebuilt_before_any_card_is_sent(self):
        self.turn("Хочу замовити чорні M")
        self.prepare("2")
        (self.root / "payments.json").write_text(json.dumps({"enabled": True, "kind": "card", "account": "4000000000000002"}))
        self.worker.send_approved()
        self.assertEqual(len(self.client.sent), 1)
        self.worker.build()
        self.worker.send_approved()
        self.assertIn("4000 0000 0000 0002", self.client.sent[-1][1])
        self.assertNotIn("4111 1111", self.client.sent[-1][1])

    def test_dispatch_day_change_rebuilds_the_answer(self):
        self.worker.dialogue.now = lambda: datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.prepare("Коли відправка?")
        self.worker.dialogue.now = None
        self.worker.send_approved()
        self.assertEqual(self.client.sent, [])
        self.worker.build()
        self.worker.send_approved()
        self.assertEqual(len(self.client.sent), 1)

    def callback(self, action, update_id):
        return {"update_id": update_id, "callback_query": {"id": "q", "from": {"id": 7}, "data": "chat:" + action,
                "message": {"message_id": 501, "chat": {"id": -100123}, "message_thread_id": 10}}}

    def test_manual_and_bot_switch_preserves_progress_done_clears_it(self):
        self.turn("Хочу замовити чорні M")
        self.owner_topic()
        self.bridge.update(self.callback("take", 1))
        self.assertEqual(self.worker.handoffs.get("account", "t")["mode"], "human")
        self.bridge.update(self.callback("bot", 2))
        self.worker.send_human_reply()
        self.assertEqual(self.worker.handoffs.get("account", "t")["mode"], "bot")
        self.assertEqual(self.store.context("account", "t", "buyer", "101", 30)["size"], "M")
        self.bridge.update(self.callback("done", 3))
        self.worker.send_human_reply()
        self.assertEqual(self.store.context("account", "t", "buyer", "101", 30), {})

    def test_idle_scan_uses_only_two_allowed_advert_lists(self):
        calls = []
        self.client.supports_advert_filter = True
        self.client.threads = lambda advert_id=None: calls.append(advert_id) or []
        self.worker.schedule.next_inbox_at = 0
        self.worker.poll()
        self.assertEqual(calls, ["101", "102"])
        self.assertEqual(self.worker.schedule.idle_interval, 10)


if __name__ == "__main__":
    unittest.main()
