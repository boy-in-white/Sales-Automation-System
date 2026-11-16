import copy
import tempfile
import unittest
from pathlib import Path

from app import AppError
from catalog import Catalog
from chat_store import Store
from olx_client import OlxClient
from product_registry import Registry, ranked_products, runtime_catalog

ROOT = Path(__file__).resolve().parents[1]


def product(product_id, name):
    return {"id": product_id, "canonical_name": name, "brand": "", "category": "", "aliases": [], "available_sizes": ["M"]}


class CatalogSyncTests(unittest.TestCase):
    def setUp(self):
        self.base = Catalog.load(ROOT / "shop.json")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.store.close_current)
        self.registry = Registry(self.store, "account")

    def test_new_ledgerly_product_needs_no_json_entry(self):
        records = [product("p1", "Штани Suvene"), product("p2", "Штани Broken Planet")]
        self.registry.snapshot(records)
        self.registry.bind("101", "p2", "Штани Broken Planet чорні", 900)
        catalog = runtime_catalog(self.base, self.registry.products(), self.registry.bindings())
        selected = catalog.products[catalog.adverts["101"]]
        self.assertEqual(selected["ledgerly_product_id"], "p2")
        self.assertEqual(selected["price_uah"], 900)
        self.assertEqual(selected["sizes"], [])
        self.assertEqual(catalog.explicit_products("є Broken Planet?")[0]["ledgerly_product_id"], "p2")

    def test_rename_keeps_stable_sql_binding(self):
        self.registry.bind("101", "p1", "Стара назва")
        old = runtime_catalog(self.base, [product("p1", "Інша модель")], self.registry.bindings())
        new = runtime_catalog(self.base, [product("p1", "Оновлена модель")], self.registry.bindings())
        self.assertEqual(old.adverts["101"], new.adverts["101"])
        self.assertEqual(new.products[new.adverts["101"]]["name"], "Оновлена модель")

    def test_deleted_product_disables_its_advert(self):
        self.registry.bind("101", "deleted", "Модель")
        catalog = runtime_catalog(self.base, [], self.registry.bindings())
        self.assertNotIn("101", catalog.adverts)

    def test_disabled_advert_does_not_return_from_json(self):
        data = copy.deepcopy(self.base.data)
        data["products"][0]["advert_ids"] = [101]
        base = Catalog(data)
        records = [product("p1", "Штани Suvene")]
        self.assertIn("101", runtime_catalog(base, records, []).adverts)
        self.registry.disable("101")
        self.assertNotIn("101", runtime_catalog(base, records, self.registry.bindings()).adverts)

    def test_matching_shared_titles_remains_ambiguous(self):
        records = [product("p1", "Штани Suvene"), product("p2", "Штани Suvene")]
        ranked = ranked_products(records, "Штани Suvene чорні", [])
        self.assertEqual(ranked[0][0], ranked[1][0])

    def test_similar_sql_names_do_not_collapse_into_one_profile(self):
        records = [product("p1", "Штани Suvene"), product("p2", "suvene")]
        catalog = runtime_catalog(self.base, records, [])
        self.assertEqual(len(catalog.products), 2)
        self.assertEqual({p["ledgerly_product_id"] for p in catalog.products.values()}, {"p1", "p2"})

    def test_choices_are_owner_scoped_single_use_and_account_scoped(self):
        data = self.registry.choice(7, {"advert": "101", "product_id": "p1"})
        token = data.split(":")[1]
        self.assertFalse(self.registry.select(8, token, "bad"))
        self.assertFalse(Registry(self.store, "other").select(7, token, "other"))
        self.assertTrue(self.registry.select(7, token, "ok"))
        self.assertFalse(self.registry.select(7, token, "again"))

    def test_changes_to_other_product_do_not_invalidate_this_draft(self):
        self.registry.bind("101", "p1", "Штани Suvene")
        before = runtime_catalog(self.base, [product("p1", "Штани Suvene")], self.registry.bindings())
        after = runtime_catalog(self.base, [product("p1", "Штани Suvene"), product("p2", "Новий товар")], self.registry.bindings())
        key = before.adverts["101"]
        self.assertEqual(before.product_signature(key, "101"), after.product_signature(key, "101"))

    def test_private_numeric_input_uses_pending_action(self):
        self.registry.private_message(7, "/add_ad", "start")
        self.registry.private_message(7, "123456789", "number")
        job = self.registry.next_job()
        self.assertEqual(job["action"], "add_ad")
        self.assertEqual(job["payload"]["argument"], "123456789")


class OwnershipTests(unittest.TestCase):
    def test_foreign_advert_is_not_read_or_enabled(self):
        class App:
            def access_token(self):
                return "fake"
            def request(self, path, payload=None, token=None):
                if path.startswith("/api/partner/adverts?"):
                    return [{"id": 101}]
                raise AssertionError("A foreign advert must not be fetched")
        with self.assertRaises(AppError):
            OlxClient(App(), minimum_interval=0).advert("999")

    def test_owned_advert_response_must_match_requested_id(self):
        class App:
            def access_token(self):
                return "fake"
            def request(self, path, payload=None, token=None):
                return [{"id": 101}] if "?" in path else {"data": {"id": 999, "title": "Wrong"}}
        with self.assertRaises(AppError):
            OlxClient(App(), minimum_interval=0).advert("101")


if __name__ == "__main__":
    unittest.main()
