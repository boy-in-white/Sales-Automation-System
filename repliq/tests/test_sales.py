"""Offline business and send-boundary checks. No OLX or PostgreSQL calls."""

import copy
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import AppError
from catalog import Catalog
from chat_store import Store
from dialogue import Dialogue
from inventory import DemoInventory, StockUnavailable
from sales_service import Worker

ROOT = Path(__file__).resolve().parents[1]


def catalog():
    data = json.loads((ROOT / "shop.json").read_text(encoding="utf-8"))
    data["products"][0]["advert_ids"] = [101]
    data["products"][0]["ledgerly_product_id"] = "pants-id"
    data["products"][1]["advert_ids"] = [102]
    data["products"][1]["ledgerly_product_id"] = "tee-id"
    for product in data["products"]:
        product['sizing_basis'] = 'body'  # Exercise the legacy verified body-chart path explicitly.
        product["size_chart_verified"] = True
        product["sizes"] = [
            {"size": "S", "height_cm": [160, 172], "weight_kg": [50, 62]},
            {"size": "M", "height_cm": [168, 178], "weight_kg": [60, 72]},
            {"size": "L", "height_cm": [174, 184], "weight_kg": [70, 84]},
            {"size": "XL", "height_cm": [180, 192], "weight_kg": [82, 98]},
        ]
    return Catalog(data)


class FakeStock(DemoInventory):
    def __init__(self):
        self.calls, self.empty, self.failed = [], False, False

    def rows(self, product):
        self.calls.append(product["key"])
        if self.failed:
            raise StockUnavailable("SQL недоступна")
        return [dict(r, available=0 if self.empty else r["available"]) for r in super().rows(product)]


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.stock = FakeStock()
        self.bot = Dialogue(catalog(), self.stock)

    def test_greeting_does_not_query_stock(self):
        p = self.bot.plan(["Добрий день"], {}, "101")
        self.assertIn("Добрий день!", p.text)
        self.assertIn("Бажаєте замовити", p.text)
        self.assertEqual(self.stock.calls, [])

    def test_russian_and_surzhyk_size_questions(self):
        for text in ("Привет, какой размер подойдет?", "який размєр підійде", "помоги подобрать размер"):
            p = self.bot.plan([text], {}, "101")
            self.assertIn("зріст", p.text)
            self.assertIn("вагу", p.text)
            self.assertEqual(p.context["product"], "suvene-pants")

    def test_split_parameters_only_ask_missing(self):
        first = self.bot.plan(["який розмір"], {}, "101")
        second = self.bot.plan(["зріст 175"], first.context, "101")
        self.assertEqual(second.context["height"], 175)
        self.assertEqual(second.context["awaiting"], "weight")
        third = self.bot.plan(["68"], second.context, "101")
        self.assertEqual(third.context["weight"], 68)
        self.assertIn("M", third.text)
        self.assertIn("stock", third.guard)

    def test_pair_and_meters(self):
        for text in ("175/68", "рост 1.75 вес 68", "175 см 68 кг"):
            p = self.bot.plan([text], {"topic": "sizing"}, "101")
            self.assertEqual(p.context["height"], 175)
            self.assertEqual(p.context["weight"], 68)
            self.assertIn("M", p.text)

    def test_overlapping_chart_prefers_available_suitable_size(self):
        p = self.bot.plan(["рост 178 вес 72"], {}, "101")
        from size_advice import preferred_available_size
        rows = DemoInventory().rows(self.bot.catalog.products['suvene-pants'])
        self.assertEqual(p.context['size'], preferred_available_size(rows,['M','L']))
        self.assertEqual(self.stock.calls,['suvene-pants'])
        self.assertNotEqual(p.context.get('awaiting'),'fit')

    def test_sql_zero_overrides_on_hand_assumptions(self):
        self.stock.empty = True
        p = self.bot.plan(["є чорні M?"], {}, "101")
        self.assertIn("немає в наявності", p.text)
        self.assertNotIn("Так,", p.text)

    def test_sql_failure_never_asserts_stock(self):
        self.stock.failed = True
        p = self.bot.plan(["є M?"], {}, "101")
        self.assertIn("актуальну наявність", p.text)
        self.assertEqual(p.guard, {})
        self.assertTrue(p.notes)

    def test_product_switch_preserves_body_but_resets_chosen_size(self):
        p = self.bot.plan(["футболка gallery який розмір"],
                          {"product": "suvene-pants", "size": "XL", "height": 175, "weight": 68}, "101")
        self.assertEqual(p.context["product"], "gallery-tee")
        self.assertEqual(p.context["size"], "M")
        self.assertEqual(self.stock.calls, ["gallery-tee"])

    def test_customer_selected_size_is_not_a_body_measurement(self):
        for saved in ({}, {"product": "gallery-tee", "topic": "sizing", "awaiting": "parameters"}):
            p = self.bot.plan(["Так, мені потрібен M розмір"], saved, "102")
            self.assertIn("наявності", p.text)
            self.assertNotIn("параметр", p.text)
            self.assertNotIn("підійде", p.text)
            self.assertNotIn("height", p.context)
            self.assertEqual(p.context["size"], "M")

    def test_unverified_size_chart_offers_measurements_without_handoff(self):
        self.bot.catalog.products["suvene-pants"]["size_chart_verified"] = False
        self.bot.catalog.products["suvene-pants"].pop("height_guidance", None)
        p = self.bot.plan(["зріст 175 вага 68"], {}, "101")
        self.assertNotIn("size", p.context)
        self.assertFalse(p.on_send.get('handoff_now'))
        self.assertNotIn("підійде M", p.text)

    def test_price_unknown_not_invented(self):
        p = self.bot.plan(["скільки коштує?"], {}, "102")
        self.assertIn("Точну суму", p.text)
        self.assertFalse(p.guard)

    def test_invalid_parameters_not_used(self):
        p = self.bot.plan(["рост 999 вес 3"], {}, "101")
        self.assertNotIn("height", p.context)
        self.assertNotIn("weight", p.context)
        self.assertEqual(self.stock.calls, [])

    def test_not_wanting_oversize_does_not_cancel_order(self):
        p = self.bot.plan(["не хочу оверсайз, звичайну посадку"],
                          {"product": "suvene-pants", "height": 178, "weight": 72, "awaiting": "fit"}, "101")
        self.assertNotEqual(p.context.get("topic"), "closed")

    def test_two_products_are_kept_separate_and_missing_details_are_asked(self):
        p = self.bot.plan(["Suvene і Gallery"], {}, "101")
        self.assertEqual(p.context["awaiting"], "cart_size")
        self.assertEqual({item["product"] for item in p.context["cart"]}, {"suvene-pants", "gallery-tee"})

    def test_requested_quantity_is_checked(self):
        p = self.bot.plan(["хочу чорні M 3 шт"], {}, "101")
        self.assertIn("недоступні", p.text)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "queue.sqlite3")
        self.addCleanup(self.store.close_current)

    def add(self, account="a", thread="t", source="m"):
        return self.store.stage(account, "s", thread, {"source_ids": [source], "text": "Вітаю"}, 900, 200)

    def test_account_thread_and_buyer_are_isolated(self):
        self.store.save_context("a", "t", "buyer1", "101", {"height": 175})
        self.assertEqual(self.store.context("a", "t", "buyer1", "101", 30), {"height": 175})
        for args in (("b", "t", "buyer1", "101"), ("a", "other", "buyer1", "101"), ("a", "t", "buyer2", "101")):
            self.assertEqual(self.store.context(*args, 30), {})

    def test_duplicate_approval_cannot_repeat(self):
        draft = self.add()
        self.assertTrue(self.store.decide(draft, "a", "s", True))
        self.assertFalse(self.store.decide(draft, "a", "s", True))
        self.assertEqual(self.add(), "handled")

    def test_new_message_invalidates_confirmation(self):
        draft = self.add()
        self.store.invalidate("a", "t")
        self.assertFalse(self.store.decide(draft, "a", "s", True))

    def test_restart_invalidates_pending_and_does_not_retry_sending(self):
        draft = self.add()
        self.store.decide(draft, "a", "s", True)
        self.store.transition(draft, "approved", "sending")
        self.store.begin("a", "new-session")
        self.assertEqual(self.store.report()[0]["status"], "uncertain")
        self.assertIsNone(self.store.next("a", "new-session", "approved"))


class FakeOlx:
    def __init__(self):
        self.history, self.sent, self.reads = [], [], 0
        self.account, self.buyer = "account", "buyer"
        self.fail_send = False

    def identity(self):
        return {"id": self.account, "name": "Test"}

    def thread(self, thread_id):
        return {"uuid": "t", "advert_id": 101, "interlocutor_id": self.buyer, "total_count": len(self.history)}

    def threads(self):
        return [self.thread("t")]

    def messages(self, thread_id):
        self.reads += 1
        return copy.deepcopy(self.history)

    def incoming(self, text="Добрий день"):
        self.history.append({"uuid": "m%s" % len(self.history), "id": len(self.history) + 1, "thread_uuid": "t", "type": "received",
                             "text": text, "created_at": datetime.now(timezone.utc).isoformat()})

    def send(self, thread_id, text):
        self.sent.append((thread_id, text))
        if self.fail_send:
            raise TimeoutError()
        return {"uuid": "sent"}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.catalog = catalog()
        self.path = root / "shop.json"
        self.path.write_text(json.dumps(self.catalog.data), encoding="utf-8")
        self.client, self.stock = FakeOlx(), FakeStock()
        self.store = Store(root / "queue.sqlite3")
        self.addCleanup(self.store.close_current)
        self.worker = Worker(self.client, self.store, self.catalog, self.path, self.stock, threading.Event())
        self.worker.initialize()

    def draft(self, text="Добрий день"):
        self.client.incoming(text)
        self.worker.collect("t", self.client.thread("t"))
        self.worker.buffers["t"]["due"] = 0
        self.worker.build()
        return self.store.next(self.worker.account, self.worker.session)

    def approve(self, draft):
        self.store.decide(draft["id"], self.worker.account, self.worker.session, True)

    def test_no_history_reads_at_startup_or_idle(self):
        self.worker.collect("t", self.client.thread("t"))
        self.assertEqual(self.client.reads, 0)

    def test_old_history_remains_untouched_on_restart(self):
        self.client.incoming("Старе непрочитане повідомлення")
        self.worker.initialize()
        self.worker.collect("t", self.client.thread("t"))
        self.worker.build()
        self.assertEqual(self.client.reads, 0)
        self.assertIsNone(self.store.next(self.worker.account, self.worker.session))

    def test_inconsistent_counter_cannot_expose_old_message(self):
        self.client.incoming("Старе повідомлення")
        self.worker.initialize()
        metadata = dict(self.client.thread("t"), total_count=2)
        with self.assertRaises(AppError):
            self.worker.collect("t", metadata)
        self.assertIsNone(self.store.next(self.worker.account, self.worker.session))

    def test_no_send_until_exact_draft_approved(self):
        draft = self.draft()
        self.worker.send_approved()
        self.assertEqual(self.client.sent, [])
        self.approve(draft)
        self.worker.send_approved()
        self.assertEqual(len(self.client.sent), 1)
        self.worker.send_approved()
        self.assertEqual(len(self.client.sent), 1)

    def test_preflight_cancels_on_new_message(self):
        draft = self.draft()
        self.approve(draft)
        self.client.incoming("Вже не треба")
        self.worker.send_approved()
        self.assertEqual(self.client.sent, [])

    def test_preflight_cancels_when_stock_changed(self):
        draft = self.draft("є чорні M?")
        self.approve(draft)
        self.stock.empty = True
        self.worker.send_approved()
        self.assertEqual(self.client.sent, [])
        self.worker.build()
        updated = self.store.next(self.worker.account, self.worker.session)
        self.assertIn("немає в наявності", updated["payload"]["text"])

    def test_sql_failure_at_final_check_gets_a_safe_reply_instead_of_silence(self):
        draft = self.draft("є чорні M?")
        self.approve(draft)
        self.stock.failed = True
        self.worker.send_approved()
        self.assertEqual(self.client.sent, [])
        self.worker.build()
        fallback = self.store.next(self.worker.account, self.worker.session)
        self.assertIn("актуальну наявність", fallback["payload"]["text"])
        self.assertFalse(fallback['payload']['on_send'].get('handoff_now'))
        self.approve(fallback)
        self.worker.send_approved()
        self.assertEqual(len(self.client.sent), 1)
        self.assertNotIn("Так,", self.client.sent[0][1])

    def test_changed_buyer_cannot_receive_previous_buyers_draft(self):
        draft = self.draft()
        self.approve(draft)
        self.client.buyer = "other"
        self.worker.send_approved()
        self.assertEqual(self.client.sent, [])

    def test_account_switch_stops_send(self):
        draft = self.draft()
        self.approve(draft)
        self.client.account = "another-account"
        with self.assertRaises(AppError):
            self.worker.send_approved()
        self.assertEqual(self.client.sent, [])

    def test_checkout_choice_is_answered_before_human_takeover(self):
        draft = self.draft("Хочу замовити чорні M")
        self.assertIn("100 грн", draft["payload"]["text"])
        self.assertIn("лише доставку", draft["payload"]["text"])
        self.approve(draft)
        self.worker.send_approved()
        self.assertIsNone(self.worker.handoffs.get("account", "t"))
        self.client.incoming("Хочу OLX доставку")
        self.worker.collect("t", self.client.thread("t"))
        self.worker.buffers["t"]["due"] = 0
        self.worker.build()
        next_draft = self.store.next(self.worker.account, self.worker.session)
        self.assertIn("очікую оформлення", next_draft["payload"]["text"])
        self.approve(next_draft)
        self.worker.send_approved()
        self.assertIsNone(self.worker.handoffs.get("account", "t"))
        self.assertIsNone(self.store.next(self.worker.account, self.worker.session))
        with self.store.db() as db:
            history = [r[0] for r in db.execute("SELECT text FROM bridge_outbox WHERE kind='history'")]
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bridge_outbox WHERE kind='document'").fetchone()[0], 0)
        self.assertEqual(history, [])
        self.assertNotIn("4111", self.client.sent[-1][1])

    def test_other_adverts_are_never_read(self):
        self.client.incoming()
        self.worker.collect("t", dict(self.client.thread("t"), advert_id=999))
        self.assertEqual(self.client.reads, 0)
        self.assertIsNone(self.store.next(self.worker.account, self.worker.session))

    def test_closed_handoff_does_not_restart_automation(self):
        self.worker.handoffs.waiting("account", "t", "buyer", "101", 0, "Test")
        self.worker.handoffs.mode("account", "t", "closed")
        self.client.incoming("Привіт")
        self.worker.collect("t", self.client.thread("t"))
        self.assertEqual(self.client.reads, 0)
        self.assertEqual(self.worker.buffers, {})

    def queue_manual_reply(self):
        self.worker.telegram_config = {"group_id": -100123, "owner_ids": [7]}
        self.worker.handoffs.waiting("account", "t", "buyer", "101", 0, "Test")
        with self.store.db() as db:
            db.execute("UPDATE handoffs SET group_id=-100123,topic_id=10,mode='human' WHERE account='account' AND thread='t'")
        route = self.worker.handoffs.get("account", "t")
        self.worker.handoffs.reply("reply-test", route, 7, "Домовилися, оформляйте в OLX")

    def test_human_reply_is_sent_once_to_bound_thread(self):
        self.queue_manual_reply()
        self.worker.send_human_reply()
        self.worker.send_human_reply()
        self.assertEqual(self.client.sent, [("t", "Домовилися, оформляйте в OLX")])

    def test_human_reply_ambiguous_post_is_not_repeated(self):
        self.queue_manual_reply()
        self.client.fail_send = True
        self.worker.send_human_reply()
        self.worker.send_human_reply()
        self.assertEqual(len(self.client.sent), 1)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT status FROM bridge_replies").fetchone()[0], "uncertain")

    def test_human_reply_blocks_changed_buyer(self):
        self.queue_manual_reply()
        self.client.buyer = "other-buyer"
        self.worker.send_human_reply()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.worker.handoffs.get("account", "t")["mode"], "blocked")

    def test_ambiguous_post_is_not_retried(self):
        draft = self.draft()
        self.approve(draft)
        self.client.fail_send = True
        self.worker.send_approved()
        self.worker.send_approved()
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.store.report()[0]["status"], "uncertain")


if __name__ == "__main__":
    unittest.main()
