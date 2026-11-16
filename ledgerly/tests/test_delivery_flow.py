import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tracking_db
import business_db
from tracking_service import apply_snapshot, process_manual_ttn, snapshot_hash


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))

    def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)


class DeliveryFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_tracking_url = tracking_db.DATABASE_URL
        self.old_business_url = business_db.BUSINESS_DATABASE_URL
        tracking_db.DB_PATH = Path(self.temp_dir.name) / "test.sqlite3"
        tracking_db.DATABASE_URL = f"sqlite:///{tracking_db.DB_PATH}"
        business_db.BUSINESS_DATABASE_URL = f"sqlite:///{tracking_db.DB_PATH}"
        tracking_db.init_db()
        business_db.init_business_db()

    def tearDown(self):
        tracking_db.DATABASE_URL = self.old_tracking_url
        business_db.BUSINESS_DATABASE_URL = self.old_business_url
        self.temp_dir.cleanup()

    def test_received_status_sends_tracking_and_1c_once(self):
        bot = FakeBot()
        shipment = tracking_db.upsert_shipment(
            "20451424252955",
            account_name="Denys",
            shipment_kind="ОЛХ",
            description="Phone case",
            cost="1090",
            cost_text="990+100",
            olx_order_id="ABC-1234",
        )
        snapshot = {
            "profile_name": "main",
            "doc": {"ActualDeliveryDate": "01.11.2026"},
            "status_label": "Отримано",
            "terminal": True,
            "status_code": "11",
            "place": "Київ, відділення 1",
        }
        snapshot["hash"] = snapshot_hash(snapshot)

        self.assertTrue(apply_snapshot(bot, shipment, snapshot))
        self.assertEqual(len(bot.sent), 2)

        current = tracking_db.get_shipment("20451424252955")
        self.assertFalse(apply_snapshot(bot, current, snapshot))
        self.assertEqual(len(bot.sent), 2)
        self.assertEqual(len(bot.edited), 0)
        self.assertEqual(tracking_db.get_shipment("20451424252955")["active"], 0)

    def test_refusal_sends_one_return_card_and_records_product(self):
        bot = FakeBot()
        shipment = tracking_db.upsert_shipment(
            "20451424252956",
            account_name="Denys",
            shipment_kind="ОЛХ",
            description="штани SUVENE",
            cost="1200",
        )
        snapshot = {
            "profile_name": "main",
            "doc": {},
            "status_label": "Відмова",
            "terminal": True,
            "status_code": "102",
            "place": "Київ",
        }
        snapshot["hash"] = snapshot_hash(snapshot)
        self.assertTrue(apply_snapshot(bot, shipment, snapshot))
        self.assertEqual(len(bot.sent), 2)
        order = business_db.get_order_by_ttn("20451424252956")
        self.assertEqual(order["status"], "REFUSED")
        self.assertEqual(order["product_name"], "Штани Suvene")
        current = tracking_db.get_shipment("20451424252956")
        self.assertFalse(apply_snapshot(bot, current, snapshot))
        self.assertEqual(len(bot.sent), 2)

    def test_new_ttn_is_queued_when_nova_poshta_has_not_seen_it_yet(self):
        bot = FakeBot()
        with patch("tracking_service.fetch_tracking_snapshot", return_value=None):
            ok, snapshot, changed, mode = process_manual_ttn(
                bot,
                "20451424252959",
                "Товар: Футболка\nЦіна: 900\n20451424252959\nНаложка",
                description="Футболка",
                shipment_kind="Наложка",
                cost_text="900",
                owner_name="Denys",
            )
        self.assertTrue(ok)
        self.assertTrue(changed)
        self.assertEqual(mode, "queued")
        self.assertEqual(snapshot["status_label"], "Очікує появи в Новій пошті")
        self.assertEqual(tracking_db.get_shipment("20451424252959")["active"], 1)
        self.assertEqual(len(bot.sent), 1)


if __name__ == "__main__":
    unittest.main()

