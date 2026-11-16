"""Automatic sending, per-customer ownership and the Telegram button interface."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import private_lock
from chat_store import Store
from handoff_store import Handoffs, quote_entities, utf16_length
from sales_service import Worker, worker_status
from telegram_bridge import Bridge
from test_sales import FakeOlx, FakeStock, catalog


class Recorder:
    def __init__(self):
        self.calls = []

    def call(self, method, fields):
        self.calls.append((method, fields))
        return {"message_id": 500 + len(self.calls)}


class AutomaticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.catalog = catalog()
        self.path = root / "shop.json"
        self.path.write_text(json.dumps(self.catalog.data), encoding="utf-8")
        self.store = Store(root / "state.sqlite3")
        self.addCleanup(self.store.close_current)
        self.client, self.stock = FakeOlx(), FakeStock()
        self.worker = Worker(self.client, self.store, self.catalog, self.path, self.stock,
                             threading.Event(), auto_send=True)
        self.worker.initialize()
        self.worker.telegram_config = {"bot_token": "test", "group_id": -100123, "owner_ids": [7]}
        self.api = Recorder()
        self.bridge = Bridge(self.worker.handoffs, self.worker.telegram_config, "account", threading.Event(), api=self.api)

    def incoming(self, text):
        self.client.incoming(text)
        self.worker.collect("t", self.client.thread("t"))
        self.worker.buffers["t"]["due"] = 0
        self.worker.build()

    def human(self):
        self.worker.handoffs.waiting("account", "t", "buyer", "101", len(self.client.history), "Customer")
        with self.store.db() as db:
            db.execute("UPDATE handoffs SET mode='human',topic_id=10,group_id=-100123 WHERE thread='t'")
            db.execute("INSERT INTO bridge_refs VALUES(-100123,110,'account','t','control')")

    def callback(self, action="done", owner=7, topic=10, message_id=110, update_id=9):
        return {"update_id": update_id, "callback_query": {"id": "q", "from": {"id": owner}, "data": "chat:" + action,
                "message": {"message_id": message_id, "chat": {"id": -100123}, "message_thread_id": topic}}}

    def test_automatic_answer_without_console_and_no_duplicate(self):
        self.incoming("Добрий день")
        self.assertIsNotNone(self.store.next("account", self.worker.session, "approved"))
        self.worker.send_approved()
        self.worker.send_approved()
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("Добрий день", self.client.sent[0][1])

    def test_closed_terminal_cannot_leave_a_false_running_status(self):
        self.assertEqual(worker_status(self.store)["status"], "stopped")
        with private_lock(self.store.path.parent / "responder-state.lock", blocking=False):
            self.assertEqual(worker_status(self.store)["status"], "running")
            data = self.store.meta("worker")
            data["heartbeat"] = 0
            self.store.set_meta("worker", data)
            self.assertEqual(worker_status(self.store)["status"], "unresponsive")

    def test_automatic_restart_does_not_answer_old_messages(self):
        self.client.incoming("Старе повідомлення")
        with patch('sales_service.Bridge') as bridge:
            self.worker.initialize()
            self.assertTrue(bridge.return_value.start.called)
        self.worker.collect("t", self.client.thread("t"))
        self.worker.build()
        self.worker.send_approved()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.client.reads, 0)

    def test_new_thread_with_olx_utc_time_is_detected(self):
        self.worker.started_at -= 2
        self.client.incoming("Привіт")
        message = self.client.history[-1]
        message["thread_uuid"] = "new-thread"
        message["created_at"] = message["created_at"].split("+")[0].split(".")[0].replace("T", " ")
        self.worker.collect("new-thread", dict(self.client.thread("t"), uuid="new-thread"))
        self.assertIn("new-thread", self.worker.buffers)

    def test_automatic_answer_still_rechecks_stock(self):
        self.incoming("є чорні M?")
        self.stock.empty = True
        self.worker.send_approved()
        self.assertEqual(self.client.sent, [])
        self.worker.build()
        self.worker.send_approved()
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("немає в наявності", self.client.sent[0][1])

    def test_done_resets_context_and_only_next_new_message_restarts_bot(self):
        self.client.incoming("Старий вибір доставки")
        self.worker.counts["t"] = 1
        self.human()
        self.store.save_context("account", "t", "buyer", "101", {"size": "XL", "order_requested": True, "checkout_offered": True})
        # An unread old message arriving before the owner's completion is also baselined.
        self.client.incoming("Старе дякую")
        update = self.callback()
        self.bridge.update(update)
        self.bridge.update(update)
        self.assertEqual(self.worker.handoffs.get("account", "t")["mode"], "returning")
        self.worker.send_human_reply()
        self.assertEqual(self.worker.handoffs.get("account", "t")["mode"], "bot")
        self.assertEqual(self.store.context("account", "t", "buyer", "101", 30), {})
        self.assertEqual(self.worker.counts["t"], 2)
        self.worker.collect("t", self.client.thread("t"))
        self.assertEqual(self.worker.buffers, {})
        self.incoming("Привіт")
        self.worker.send_approved()
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("Бажаєте замовити", self.client.sent[0][1])
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bridge_replies WHERE kind='return_bot'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT status FROM bridge_replies WHERE kind='return_bot'").fetchone()[0], "sent")

    def test_new_order_reuses_topic_and_returns_to_human(self):
        self.human()
        self.bridge.update(self.callback())
        self.worker.send_human_reply()
        self.incoming("Хочу замовити чорні M")
        self.worker.send_approved()
        self.assertEqual(self.worker.handoffs.get("account", "t")["mode"], "bot")
        self.client.incoming("Обираю OLX Доставку")
        self.worker.collect("t", self.client.thread("t"))
        self.worker.buffers["t"]["due"] = 0
        self.worker.build()
        self.worker.send_approved()
        route = self.worker.handoffs.get("account", "t")
        self.assertEqual((route["mode"], route["topic_id"]), ("bot", 10))

    def test_other_owner_topic_and_untrusted_message_cannot_press_buttons(self):
        self.human()
        for args in ({"owner": 8}, {"topic": 20}, {"message_id": 222}):
            self.bridge.update(self.callback(**args))
        self.assertEqual(self.worker.handoffs.get("account", "t")["mode"], "human")
        self.assertIsNone(self.worker.handoffs.next_reply("account"))

    def test_pause_and_resume_buttons_are_scoped_to_one_customer(self):
        self.human()
        self.worker.handoffs.waiting("account", "other", "other-buyer", "101", 0, "Other")
        self.bridge.update(self.callback("pause"))
        self.assertEqual(self.worker.handoffs.get("account", "t")["mode"], "paused")
        self.assertEqual(self.worker.handoffs.get("account", "other")["mode"], "waiting")
        self.bridge.update(self.callback("resume", update_id=10))
        self.assertEqual(self.worker.handoffs.get("account", "t")["mode"], "human")

    def test_human_takeover_invalidates_queued_automatic_answer(self):
        self.human()
        self.bridge.update(self.callback())
        self.worker.send_human_reply()
        self.incoming("Привіт")
        self.bridge.update(self.callback("take", update_id=10))
        self.worker.send_approved()
        self.assertEqual(self.client.sent, [])

    def test_history_is_quoted_in_chat_and_preserves_roles_and_literal_text(self):
        self.human()
        history = [{"_id": "buyer", "uuid": "buyer", "type": "received", "text": "Привіт <b>текст</b> 😊"},
                   {"_id": "bot", "uuid": "bot", "type": "sent", "text": "Вітаю 😊"},
                   {"_id": "owner", "uuid": "owner", "type": "sent", "text": "Оформляю"}]
        self.worker.handoffs.mark_sent("account", "t", history[1], "bot")
        self.worker.handoffs.mark_sent("account", "t", history[2], "owner")
        self.worker.handoffs.history("account", "t", history, "history-test")
        self.bridge.deliver()
        method, fields = self.api.calls[-1]
        self.assertEqual(method, "sendMessage")
        self.assertIn("👤 Клієнт", fields["text"])
        self.assertIn("🤖 Бот", fields["text"])
        self.assertIn("✍️ Ти", fields["text"])
        self.assertIn("<b>текст</b>", fields["text"])
        self.assertNotIn("parse_mode", fields)
        self.assertEqual(sum(e["type"] == "blockquote" for e in fields["entities"]), 3)

    def test_successful_owner_reply_is_not_echoed_again_from_olx(self):
        self.human()
        sent = {"uuid": "ABC-def", "id": 71}
        self.worker.handoffs.mark_sent("account", "t", sent, "owner", seen=True)
        self.worker.handoffs.append("account", "t", [{"_id": "abc-DEF", "uuid": "abc-DEF", "id": 71,
                                                     "type": "sent", "text": "Відповідь"}], 2)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bridge_outbox WHERE kind='seller'").fetchone()[0], 0)

    def test_pending_legacy_txt_is_migrated_to_chat_messages(self):
        self.human()
        with self.store.db() as db:
            self.worker.handoffs.enqueue(db, "old-txt", "account", "t", "document", "Клієнт\nСтара історія")
        self.worker.handoffs.recover("account")
        self.bridge.deliver()
        self.assertEqual(self.api.calls[-1][0], "sendMessage")
        self.assertIn("Стара історія", self.api.calls[-1][1]["text"])
        self.assertTrue(self.api.calls[-1][1]["entities"])

    def test_existing_closed_topic_gets_buttons_without_silently_reopening(self):
        self.human()
        self.worker.handoffs.mode("account", "t", "closed")
        self.worker.handoffs.refresh_controls("account")
        self.bridge.deliver()
        markup = next(fields["reply_markup"] for method, fields in self.api.calls if method == "sendMessage")
        self.assertIn("auto", [b["text"] for row in markup["inline_keyboard"] for b in row])
        self.assertEqual(self.worker.handoffs.get("account", "t")["mode"], "closed")

    def test_private_menu_buttons_support_edit_remove_and_cancel(self):
        registry = self.bridge.registry
        registry.private_message(7, "✏️ Змінити оголошення", "edit-start")
        registry.private_message(7, "101 Gallery Dept", "edit-number")
        job = registry.next_job()
        self.assertEqual(job["action"], "edit_ad")
        self.assertEqual(job["payload"]["argument"], "101 Gallery Dept")
        registry.private_message(7, "⛔ Вимкнути оголошення", "remove-start")
        registry.private_message(7, "↩️ Скасувати", "cancel")
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0], 0)

    def test_quotes_use_utf16_offsets_and_large_history_is_batched(self):
        blocks = ["🤖 Бот\nВітаю 😊", "👤 Клієнт\nДякую"]
        text, entities = quote_entities(blocks)
        second = [e for e in entities if e["type"] == "blockquote"][1]
        self.assertEqual(second["offset"], utf16_length(blocks[0] + "\n\n"))
        self.assertEqual(second["length"], utf16_length(blocks[1]))
        self.human()
        history = [{"_id": str(i), "type": "received", "text": "😊" * 2000} for i in range(4)]
        self.worker.handoffs.history("account", "t", history, "long")
        with self.store.db() as db:
            parts = [json.loads(r[0]) for r in db.execute("SELECT text FROM bridge_outbox WHERE kind='history'")]
        self.assertGreater(len(parts), 1)
        self.assertEqual(sum("".join(p).count("😊") for p in parts), 8000)
        self.assertTrue(all(utf16_length("\n\n".join(p)) <= 3400 for p in parts))
        self.assertTrue(all(block.startswith("👤 Клієнт\n") for batch in parts for block in batch))


if __name__ == "__main__":
    unittest.main()
