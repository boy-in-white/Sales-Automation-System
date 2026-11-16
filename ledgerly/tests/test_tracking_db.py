import tempfile
import unittest
from pathlib import Path

import tracking_db


class TrackingDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_database_url = tracking_db.DATABASE_URL
        tracking_db.DB_PATH = Path(self.temp_dir.name) / "test.sqlite3"
        tracking_db.DATABASE_URL = f"sqlite:///{tracking_db.DB_PATH}"
        tracking_db.init_db()

    def tearDown(self):
        tracking_db.DATABASE_URL = self.old_database_url
        self.temp_dir.cleanup()

    def test_upsert_preserves_existing_nonempty_metadata(self):
        tracking_db.upsert_shipment(
            "20451424252955",
            account_name="Denys",
            shipment_kind="ОЛХ",
            description="Phone case",
        )
        tracking_db.upsert_shipment("20451424252955")
        row = tracking_db.get_shipment("20451424252955")
        self.assertEqual(row["account_name"], "Denys")
        self.assertEqual(row["shipment_kind"], "ОЛХ")
        self.assertEqual(row["description"], "Phone case")

    def test_group_message_claim_is_idempotent(self):
        self.assertTrue(tracking_db.claim_group_message(-100, 12, 99))
        self.assertFalse(tracking_db.claim_group_message(-100, 12, 99))
        tracking_db.complete_group_message(-100, 12, 99)
        self.assertTrue(tracking_db.seen_group_message(-100, 12, 99))
        self.assertFalse(tracking_db.claim_group_message(-100, 12, 99))

    def test_1c_reservation_prevents_duplicate_send(self):
        tracking_db.upsert_shipment("20451424252955")
        self.assertTrue(tracking_db.reserve_1c_delivery("20451424252955"))
        self.assertFalse(tracking_db.reserve_1c_delivery("20451424252955"))
        tracking_db.finish_1c_delivery("20451424252955", success=True)
        self.assertFalse(tracking_db.reserve_1c_delivery("20451424252955"))

    def test_tracking_card_reservation_prevents_duplicate_send(self):
        tracking_db.upsert_shipment("20451424252956")
        state, message_id = tracking_db.reserve_tracking_message_slot("20451424252956")
        self.assertEqual((state, message_id), ("claimed", None))
        self.assertEqual(
            tracking_db.reserve_tracking_message_slot("20451424252956"),
            ("busy", None),
        )

    def test_csv_neutralizes_spreadsheet_formulas(self):
        tracking_db.upsert_shipment(
            "20451424252955",
            description="=HYPERLINK(\"bad\")",
        )
        exported = tracking_db.export_shipments_csv(1).decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", exported)

    def test_failure_counter_is_incremented_and_missing_ttn_is_safe(self):
        tracking_db.upsert_shipment("20451424252955")
        self.assertEqual(tracking_db.record_check_failure("20451424252955", "one"), 1)
        self.assertEqual(tracking_db.record_check_failure("20451424252955", "two"), 2)
        row = tracking_db.get_shipment("20451424252955")
        self.assertEqual(row["consecutive_failures"], 2)
        self.assertEqual(row["last_error"], "two")
        self.assertGreater(row["next_check_at"], 0)
        self.assertEqual(tracking_db.record_check_failure("missing", "ignored"), 0)

    def test_unknown_update_field_fails_loudly(self):
        tracking_db.upsert_shipment("20451424252955")
        with self.assertRaisesRegex(ValueError, "Unknown shipment field"):
            tracking_db.update_shipment("20451424252955", typo_field="value")


if __name__ == "__main__":
    unittest.main()

