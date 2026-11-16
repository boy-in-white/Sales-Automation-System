import tempfile
import threading
import time
import unittest
from pathlib import Path

from chat_store import Store
from handoff_store import Handoffs
from telegram_bridge import Bridge, TelegramError


class NoNetwork:
    def call(self, *args):
        raise AssertionError("Network not expected")


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.store.close_current)
        self.routes = Handoffs(self.store)
        self.config = {"bot_token": "test", "owner_ids": [7], "group_id": -100123}
        self.bridge = Bridge(self.routes, self.config, "account", threading.Event(), api=NoNetwork())
        self.bridge.me = {"id": 1, "username": "RepliqTestBot"}
        with self.store.db() as db:
            db.execute("INSERT INTO effective_ads VALUES('account','101','product-id')")
        for thread, topic, buyer in (("a", 10, "buyer-a"), ("b", 20, "buyer-b")):
            self.routes.waiting("account", thread, buyer, "101", 1, thread)
            with self.store.db() as db:
                db.execute("UPDATE handoffs SET group_id=?,topic_id=?,mode='human',topic_state='ready' WHERE thread=?",
                           (-100123, topic, thread))
                db.execute("INSERT INTO bridge_refs VALUES(?,?,?,?,?)", (-100123, topic + 100, "account", thread, "customer"))

    def message(self, text="Доброго дня", owner=7, topic=10, reply=110):
        message = {"message_id": 999, "date": int(time.time()), "from": {"id": owner},
                   "chat": {"id": -100123}, "message_thread_id": topic, "text": text}
        if reply:
            message["reply_to_message"] = {"message_id": reply}
        return {"update_id": 100, "message": message}

    def test_reply_routes_to_exact_customer(self):
        self.bridge.update(self.message())
        item = self.routes.next_reply("account")
        self.assertEqual(item["thread"], "a")
        self.assertEqual(item["text"], "Доброго дня")

    def test_other_owner_and_unmapped_topic_are_ignored(self):
        self.bridge.update(self.message(owner=8))
        self.bridge.update(self.message(topic=33))
        self.bridge.update(self.message(topic=20, reply=110))
        self.assertIsNone(self.routes.next_reply("account"))

    def test_general_topic_and_explicit_notes_never_leave_telegram(self):
        self.bridge.update(self.message("/note Нотатка для себе", reply=None))
        self.bridge.update(self.message("/say Не клієнту", topic=1, reply=None))
        self.assertIsNone(self.routes.next_reply("account"))

    def test_plain_text_in_customer_topic_is_a_reply(self):
        self.bridge.update(self.message("Вітаю, допоможу оформити 😊", reply=None))
        self.assertEqual(self.routes.next_reply("account")["text"], "Вітаю, допоможу оформити 😊")

    def test_say_is_explicit_customer_message(self):
        self.bridge.update(self.message("/say Обирайте OLX Доставку", topic=20, reply=None))
        item = self.routes.next_reply("account")
        self.assertEqual(item["thread"], "b")
        self.assertEqual(item["text"], "Обирайте OLX Доставку")

    def test_updates_are_idempotent(self):
        update = self.message()
        self.bridge.update(update)
        self.bridge.update(update)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bridge_replies").fetchone()[0], 1)

    def test_forwarded_bot_and_stale_messages_are_ignored(self):
        for field, value in (("forward_origin", {"type": "user"}), ("sender_chat", {"id": -1}), ("date", 0)):
            update = self.message()
            update["message"][field] = value
            self.bridge.update(update)
        self.assertIsNone(self.routes.next_reply("account"))

    def test_commands_for_other_bot_are_ignored(self):
        self.bridge.update(self.message("/say@OtherBot text", reply=None))
        self.assertIsNone(self.routes.next_reply("account"))

    def test_pause_cancels_pending_customer_replies(self):
        self.bridge.update(self.message())
        update = self.message("/pause", reply=None)
        update["update_id"] = 101
        self.bridge.update(update)
        self.assertEqual(self.routes.get("account", "a")["mode"], "paused")
        self.assertIsNone(self.routes.next_reply("account"))

    def test_done_returns_route_to_bot_without_sending_operator_text(self):
        self.bridge.update(self.message("/done", reply=None))
        self.assertEqual(self.routes.get("account", "a")["mode"], "returning")
        update = self.message("/note внутрішня нотатка", reply=None)
        update["update_id"] = 102
        self.bridge.update(update)
        self.assertEqual(self.routes.next_reply("account")["kind"], "return_bot")
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bridge_replies WHERE kind='reply'").fetchone()[0], 0)

    def test_sending_state_recovers_as_uncertain(self):
        self.bridge.update(self.message())
        item = self.routes.next_reply("account")
        self.routes.finish_reply(item["event_key"], "sending")
        self.routes.recover("account")
        self.assertIsNone(self.routes.next_reply("account"))
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT status FROM bridge_replies").fetchone()[0], "uncertain")

    def test_topic_create_timeout_is_not_retried(self):
        self.routes.waiting("account", "new", "buyer-new", "101", 1, "New")
        with self.store.db() as db:
            db.execute("UPDATE handoffs SET mode='opening' WHERE thread='new'")
        class Failure:
            calls = 0
            def call(self, *args):
                self.calls += 1
                raise TelegramError()
        api = Failure()
        self.bridge.api = api
        with self.assertRaises(TelegramError):
            self.bridge.create_topic()
        self.assertFalse(self.bridge.create_topic())
        self.assertEqual(api.calls, 1)

    def test_only_owner_can_use_private_admin(self):
        update = self.message("/add_ad 123")
        update["message"]["chat"] = {"id": 8, "type": "private"}
        update["message"]["from"] = {"id": 8}
        self.bridge.update(update)
        self.assertIsNone(self.bridge.registry.next_job())
        update["message"]["chat"]["id"] = 7
        update["message"]["from"]["id"] = 7
        self.bridge.update(update)
        self.assertEqual(self.bridge.registry.next_job()["owner"], 7)

    def test_catalog_admin_commands_in_group_are_ignored(self):
        self.bridge.update(self.message("/add_ad 123", reply=None))
        self.assertIsNone(self.bridge.registry.next_job())

    def test_disabled_advert_does_not_block_other_customer_delivery(self):
        self.routes.notice("account", "a", "Disabled", "disabled")
        self.routes.notice("account", "b", "Allowed", "allowed")
        with self.store.db() as db:
            db.execute("DELETE FROM effective_ads")
            db.execute("INSERT INTO effective_ads VALUES('account','102','product-b')")
            db.execute("UPDATE handoffs SET advert='102' WHERE thread='b'")
        class Recorder:
            fields = None
            def call(self, method, fields):
                self.fields = fields
                return {"message_id": 555}
        api = Recorder()
        self.bridge.api = api
        self.bridge.deliver()
        self.assertEqual(api.fields["message_thread_id"], 20)
        self.assertEqual(api.fields["text"], "Allowed")


if __name__ == "__main__":
    unittest.main()
