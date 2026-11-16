import tempfile
import unittest
from pathlib import Path

import business_db


class BusinessDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        path = Path(self.temp_dir.name) / "business.sqlite3"
        self.old_database_url = business_db.BUSINESS_DATABASE_URL
        business_db.BUSINESS_DATABASE_URL = f"sqlite:///{path}"
        business_db.init_business_db()

    def tearDown(self):
        business_db.BUSINESS_DATABASE_URL = self.old_database_url
        self.temp_dir.cleanup()

    def test_product_word_order_resolves_to_one_product(self):
        first = business_db.resolve_product("штани SUVENE")
        second = business_db.resolve_product("suvene Штани")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["canonical_name"], "Штани Suvene")

    def test_sales_and_refusals_are_recorded(self):
        business_db.record_order(
            ttn="20451424252955",
            sale_type_label="ОЛХ",
            carrier="Nova Poshta",
            product_name="штани SUVENE",
            sale_price="1200",
            purchase_cost="500",
            delivery_cost="100",
            platform_fee="50",
            initial_status="DELIVERED",
        )
        business_db.record_order(
            ttn="20451424252956",
            sale_type_label="Наложка",
            carrier="Nova Poshta",
            product_name="suvene Штани",
            sale_price="1200",
            purchase_cost="500",
        )
        business_db.update_order_status_by_ttn("20451424252956", "REFUSED")
        delivered = business_db.get_order_by_ttn("20451424252955")
        refused = business_db.get_order_by_ttn("20451424252956")
        self.assertEqual(delivered["status"], "DELIVERED")
        self.assertEqual(delivered["sale_price_cents"], 120000)
        self.assertEqual(delivered["purchase_cost_cents"], 50000)
        self.assertEqual(delivered["product_name"], "Штани Suvene")
        self.assertEqual(refused["status"], "REFUSED")
        self.assertIsNotNone(refused["refused_at"])

    def test_external_monthly_turnover_can_be_read(self):
        business_db.upsert_monthly_turnover(
            "2026-11",
            "125000.50",
            orders_count=87,
            source="analytics-service",
        )
        result = business_db.get_monthly_turnover("2026-11")
        self.assertEqual(result["turnover_cents"], 12500050)
        self.assertEqual(result["orders_count"], 87)
        self.assertEqual(result["source"], "analytics-service")

    def test_live_all_time_stats(self):
        business_db.record_order(
            ttn="20451424252970",
            sale_type_label="ОЛХ",
            carrier="Nova Poshta",
            product_name="Штани",
            sale_price="1200",
            purchase_cost="500",
            delivery_cost="100",
            platform_fee="50",
            initial_status="DELIVERED",
        )
        business_db.record_order(
            ttn="20451424252971",
            sale_type_label="Офлайн",
            carrier="Offline",
            product_name="Футболка",
            quantity=2,
            sale_price="800",
            purchase_cost="300",
            initial_status="DELIVERED",
        )
        refused = business_db.record_order(
            ttn="20451424252972",
            sale_type_label="Наложка",
            carrier="Nova Poshta",
            product_name="Куртка",
            sale_price="900",
        )
        business_db.update_order_status(refused["id"], "REFUSED")

        stats = business_db.get_sales_stats("all")
        self.assertEqual(stats["sales_count"], 2)
        self.assertEqual(stats["items_count"], 3)
        self.assertEqual(stats["turnover_cents"], 200000)
        self.assertEqual(stats["expenses_cents"], 95000)
        self.assertEqual(stats["profit_cents"], 105000)
        self.assertEqual(stats["refusals_count"], 1)

    def test_current_month_reset_keeps_orders_and_older_turnover(self):
        current_month = business_db.current_month_label()
        business_db.record_order(
            ttn="20451424252974",
            sale_type_label="ОЛХ",
            carrier="Nova Poshta",
            product_name="Штани",
            sale_price="1200",
            initial_status="DELIVERED",
        )
        business_db.upsert_monthly_turnover(current_month, "1200", orders_count=1)
        business_db.upsert_monthly_turnover("2025-01", "500", orders_count=1)

        result = business_db.clear_current_month_analytics(123, "Owner")

        self.assertEqual(result["month"], current_month)
        self.assertEqual(business_db.get_sales_stats("month")["turnover_cents"], 0)
        self.assertIsNone(business_db.get_monthly_turnover(current_month))
        self.assertEqual(
            business_db.get_monthly_turnover("2025-01")["turnover_cents"],
            50000,
        )
        self.assertEqual(business_db.business_health()["orders"], 1)

        business_db.record_order(
            ttn="20451424252975",
            sale_type_label="Офлайн",
            carrier="Offline",
            product_name="Футболка",
            sale_price="800",
            initial_status="DELIVERED",
        )
        self.assertEqual(business_db.get_sales_stats("month")["turnover_cents"], 80000)

    def test_submission_claim_is_atomic(self):
        submission_id = business_db.create_submission(
            "Товар: Штани Suvene\nЦіна: 1200\nОфлайн",
            {"shipment_kind": "Офлайн"},
            [],
            123,
            "Denys",
        )
        self.assertTrue(business_db.claim_submission(submission_id, 777))
        self.assertFalse(business_db.claim_submission(submission_id, 888))
        business_db.finish_submission(submission_id, True)
        self.assertEqual(business_db.get_submission(submission_id)["status"], "CONFIRMED")

    def test_real_shipment_confirmation_stores_time_and_actor(self):
        business_db.record_order(
            ttn="20451424252957",
            sale_type_label="Наложка",
            carrier="Nova Poshta",
            product_name="Штани Suvene",
            sale_price="1200",
            initial_status="CONFIRMED",
        )
        shipped = business_db.mark_order_shipped_by_ttn(
            "20451424252957",
            123,
            "Denys",
        )
        self.assertEqual(shipped["status"], "SHIPPED")
        self.assertIsNotNone(shipped["shipped_at"])
        with business_db._transaction() as session:
            event = session.one(
                "SELECT * FROM order_events WHERE order_id = ? "
                "AND event_type = 'SHIPMENT_CONFIRMED_BY_USER'",
                (shipped["id"],),
            )
        self.assertEqual(event["actor_user_id"], 123)

    def test_shipment_button_does_not_regress_delivered_order(self):
        delivered = business_db.record_order(
            ttn="20451424252958",
            sale_type_label="ОЛХ",
            carrier="Nova Poshta",
            product_name="Футболка",
            sale_price="900",
            initial_status="DELIVERED",
        )
        result = business_db.mark_order_shipped_by_ttn(
            delivered["ttn"],
            123,
            "Denys",
        )
        self.assertEqual(result["status"], "DELIVERED")

    def test_notification_claim_is_not_duplicated(self):
        order = business_db.record_order(
            ttn="20451424252960",
            sale_type_label="ОЛХ",
            carrier="Nova Poshta",
            product_name="Футболка",
            sale_price="900",
        )
        self.assertTrue(business_db.claim_notification(order["id"], "1C"))
        self.assertFalse(business_db.claim_notification(order["id"], "1C"))
        business_db.finish_notification(order["id"], "1C", False)
        self.assertTrue(business_db.claim_notification(order["id"], "1C"))
        notification = business_db.get_notification(order["id"], "1C")
        self.assertEqual(notification["status"], "CLAIMED")


if __name__ == "__main__":
    unittest.main()

