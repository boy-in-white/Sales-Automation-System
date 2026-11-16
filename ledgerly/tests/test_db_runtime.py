import unittest

import business_db
from db_runtime import Session, database_backend, redacted_database_url


class DatabaseRuntimeTests(unittest.TestCase):
    def test_backend_detection(self):
        self.assertEqual(database_backend("postgresql://u:p@localhost/db"), "postgresql")
        self.assertEqual(database_backend("sqlite:///./data/test.sqlite3"), "sqlite")

    def test_postgres_placeholder_translation(self):
        session = Session(None, "postgresql")
        self.assertEqual(
            session._sql("SELECT * FROM orders WHERE id = ? AND status = ?"),
            "SELECT * FROM orders WHERE id = %s AND status = %s",
        )
        self.assertEqual(
            session._sql("SELECT :ttn, :source_user_id"),
            "SELECT %(ttn)s, %(source_user_id)s",
        )

    def test_database_url_is_redacted(self):
        visible = redacted_database_url(
            "postgresql://olx_bot:very-secret@127.0.0.1:5432/olx_business"
        )
        self.assertNotIn("very-secret", visible)
        self.assertIn("olx_bot:***@", visible)

    def test_postgres_schema_uses_separate_analytics_namespace(self):
        schema = "\n".join(business_db._schema_statements("postgresql"))
        self.assertIn("analytics.monthly_turnover", schema)
        self.assertIn("analytics.monthly_analytics_resets", schema)
        self.assertIn("analytics.v_monthly_sales", schema)
        self.assertIn("analytics.v_inventory_stock", schema)
        self.assertIn("analytics.v_inventory_movements", schema)
        self.assertIn("analytics.v_inventory_batches", schema)


if __name__ == "__main__":
    unittest.main()

