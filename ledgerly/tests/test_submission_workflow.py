import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import business_db
import callbacks
import handlers
import tracking_db


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.answers = []
        self.deleted = []

    def send_message(self, *args, **kwargs):
        chat_id = args[0] if args else kwargs.get("chat_id")
        text = args[1] if len(args) > 1 else kwargs.get("text")
        message = SimpleNamespace(
            message_id=len(self.sent) + 1,
            chat=SimpleNamespace(id=chat_id),
            text=text,
            reply_markup=kwargs.get("reply_markup"),
        )
        self.sent.append(message)
        return message

    def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)

    def answer_callback_query(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class SubmissionWorkflowTests(unittest.TestCase):
    def test_private_order_keeps_product_and_shows_actual_waybill(self):
        bot = FakeBot()
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private"),
            from_user=SimpleNamespace(id=123, full_name="Denys", username="denys"),
            text="ФОП\n0504947958303\nШтани suvene чорні S\n1134 грн",
            caption=None,
        )
        handlers.handle_private_message(bot, message)
        self.assertIn("Опис: Штани suvene чорні S", bot.sent[0].text)
        self.assertIn("Ціна продажу: 1134", bot.sent[0].text)
        self.assertIn("0504947958303", bot.sent[-1].text)
        self.assertIn("Підтвердити замовлення", bot.sent[-1].text)
        self.assertNotIn("Опис: 1134", bot.sent[0].text)
        self.assertEqual(business_db.business_health()["orders"], 0)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        path = Path(self.temp_dir.name) / "bot.sqlite3"
        self.old_tracking_url = tracking_db.DATABASE_URL
        self.old_business_url = business_db.BUSINESS_DATABASE_URL
        business_db.BUSINESS_DATABASE_URL = f"sqlite:///{path}"
        tracking_db.DB_PATH = path
        tracking_db.DATABASE_URL = f"sqlite:///{path}"
        business_db.init_business_db()
        tracking_db.init_db()
        self.old_allowed = handlers.is_private_user_allowed
        self.old_admin = handlers.is_admin
        self.old_owner = handlers.is_owner
        self.old_analytics_access = handlers.ANALYTICS_ACCESS
        self.old_employee = callbacks.is_employee
        self.old_callback_owner = callbacks.is_owner
        self.old_callback_analytics_access = callbacks.ANALYTICS_ACCESS
        handlers.is_private_user_allowed = lambda _user_id: True
        handlers.is_admin = lambda _user_id: True
        handlers.is_owner = lambda _user_id: True
        handlers.ANALYTICS_ACCESS = SimpleNamespace(
            is_unlocked=lambda _user_id: True,
            lock=lambda _user_id: True,
        )
        callbacks.is_employee = lambda _user_id: True
        callbacks.is_owner = lambda _user_id: True
        callbacks.ANALYTICS_ACCESS = handlers.ANALYTICS_ACCESS

    def tearDown(self):
        handlers.is_private_user_allowed = self.old_allowed
        handlers.is_admin = self.old_admin
        handlers.is_owner = self.old_owner
        handlers.ANALYTICS_ACCESS = self.old_analytics_access
        callbacks.is_employee = self.old_employee
        callbacks.is_owner = self.old_callback_owner
        callbacks.ANALYTICS_ACCESS = self.old_callback_analytics_access
        tracking_db.DATABASE_URL = self.old_tracking_url
        business_db.BUSINESS_DATABASE_URL = self.old_business_url
        self.temp_dir.cleanup()

    def test_offline_draft_requires_button_then_becomes_sale(self):
        bot = FakeBot()
        user = SimpleNamespace(id=123, full_name="Denys", username="denys")
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private"),
            from_user=user,
            text="Іван\nТовар: suvene Штани\nЦіна: 1200\nСобівартість: 500\nОфлайн",
            caption=None,
        )
        handlers.handle_private_message(bot, message)
        self.assertEqual(len(bot.sent), 2)
        draft_message = bot.sent[0]
        self.assertIsNotNone(draft_message.reply_markup)
        self.assertEqual(business_db.business_health()["orders"], 0)

        callback_data = draft_message.reply_markup.keyboard[0][0].callback_data
        call = SimpleNamespace(
            id="callback-1",
            data=callback_data,
            from_user=SimpleNamespace(id=777, full_name="Employee", username=None),
            message=draft_message,
        )
        callbacks.handle_callback(bot, call)

        self.assertEqual(business_db.business_health()["orders"], 1)
        self.assertEqual(business_db.list_products()[0]["canonical_name"], "Штани Suvene")
        self.assertTrue(bot.edited)

    def test_turnover_command_reads_external_result(self):
        business_db.upsert_monthly_turnover(
            "2026-11",
            "125000.50",
            orders_count=87,
            source="analytics-service",
        )
        bot = FakeBot()
        user = SimpleNamespace(id=123, full_name="Denys", username="denys")
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private"),
            from_user=user,
            text="/turnover 2026-11",
            caption=None,
        )
        handlers.handle_private_message(bot, message)
        self.assertIn("125000.50 UAH", bot.sent[0].text)
        self.assertIn("Замовлень: 87", bot.sent[0].text)

    def test_financial_command_is_hidden_while_locked(self):
        handlers.ANALYTICS_ACCESS = SimpleNamespace(
            is_unlocked=lambda _user_id: False,
            lock=lambda _user_id: True,
        )
        bot = FakeBot()
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private"),
            from_user=SimpleNamespace(id=123, full_name="Denys", username="denys"),
            text="/stats all",
            caption=None,
        )
        handlers.handle_private_message(bot, message)
        self.assertIn("Фінанси закриті", bot.sent[0].text)

    def test_clear_month_cannot_target_an_older_month(self):
        current_month = business_db.current_month_label()
        business_db.upsert_monthly_turnover(current_month, "1200")
        business_db.upsert_monthly_turnover("2025-01", "500")
        bot = FakeBot()
        user = SimpleNamespace(id=123, full_name="Denys", username="denys")
        invalid = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private"),
            from_user=user,
            text="/clear_month 2025-01",
            caption=None,
        )
        handlers.handle_private_message(bot, invalid)
        self.assertIn("тільки з поточним місяцем", bot.sent[-1].text)
        self.assertIsNotNone(business_db.get_monthly_turnover("2025-01"))

        valid = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private"),
            from_user=user,
            text="/clear_month",
            caption=None,
        )
        handlers.handle_private_message(bot, valid)
        confirmation = bot.sent[-1]
        self.assertIsNotNone(confirmation.reply_markup)
        call = SimpleNamespace(
            id="clear-current",
            data=confirmation.reply_markup.keyboard[0][0].callback_data,
            from_user=user,
            message=confirmation,
        )
        callbacks.handle_callback(bot, call)
        self.assertIsNone(business_db.get_monthly_turnover(current_month))
        self.assertIsNotNone(business_db.get_monthly_turnover("2025-01"))

    def test_1c_button_writes_stock_off_and_reports_sold_out(self):
        product = business_db.create_inventory_product(
            "Футболка Gallery Dept",
            ["M"],
            ["BLACK"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.change_inventory_stock(variant["id"], 1, 123, "Denys")
        order = business_db.record_order(
            ttn="20451424252991",
            sale_type_label="ОЛХ",
            carrier="Nova Poshta",
            product_name="Gallery Dept футболка чорна M",
        )
        bot = FakeBot()
        call = SimpleNamespace(
            id="onec-stock",
            data=f"ord:1c:{order['id']}",
            from_user=SimpleNamespace(id=123, full_name="Denys", username="denys"),
            message=SimpleNamespace(
                message_id=10,
                chat=SimpleNamespace(id=-100),
                text="Картка 1С",
            ),
        )

        callbacks.handle_callback(bot, call)

        self.assertEqual(business_db.get_inventory_variant(variant["id"])["on_hand"], 0)
        self.assertTrue(any("Товар закінчився" in message.text for message in bot.sent))
        self.assertIn("Склад: −1 шт.", bot.edited[-1]["text"])


if __name__ == "__main__":
    unittest.main()

