import tempfile
import unittest

import business_db as db
from product_link_repair import repair_product_links


class ProductIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old = db.BUSINESS_DATABASE_URL
        self.addCleanup(setattr, db, 'BUSINESS_DATABASE_URL', old)
        db.BUSINESS_DATABASE_URL = 'sqlite:///' + self.temp.name + '/identity.sqlite3'
        db.init_business_db()
        self.product = db.create_inventory_product('Футболки Gallery Dept', ['S','M'], ['BLACK','WHITE'])
        self.variants = db.list_product_variants(self.product['id'])
        for row in self.variants:
            db.set_product_transport_cost(self.product['id'], 'AIR', '360', 1)
            db.receive_inventory_batch(row['id'], 'AIR', 5, 1)

    def order(self, name, ttn='test'):
        return db.record_order(ttn=ttn, sale_type_label='ОЛХ', carrier='Nova Poshta',
                               product_name=name, sale_price='513', initial_status='DELIVERED')

    def test_variant_descriptions_share_the_base_product_in_sales_and_stock(self):
        names = ('Gallery dept чорна S', 'Чорна Gallery Dept S', 'Футболка Gallery Dept чорний S',
                 'Gallery Dept футболки колір білий розмір М', 'GALLERY DEPT BLACK M',
                 'Gallery dept белая С')
        for i, name in enumerate(names):
            order = self.order(name, str(i))
            self.assertEqual(order['product_id'], self.product['id'], name)
        self.assertEqual(len(db.list_inventory_products()), 1)
        self.assertEqual(len(db.list_products()), 1)

    def test_unknown_or_explicitly_wrong_category_creates_no_phantom_sku(self):
        for i, name in enumerate(('Штани Gallery Dept чорні S', 'Футболка Another Brand чорна S',
                                  'Футболка Gallery Dept Limited чорна S')):
            order = self.order(name, str(i))
            self.assertIsNone(order['product_id'], name)
            result = db.mark_order_in_1c(order['id'], 1)
            self.assertEqual(result['inventory_status'], 'unmatched')
        self.assertEqual(len(db.list_products()), 1)
        self.assertTrue(all(v['on_hand'] == 5 for v in db.list_product_variants(self.product['id'])))

    def test_same_brand_different_products_need_explicit_category(self):
        pants = db.create_inventory_product('Штани Gallery Dept', ['S'], ['BLACK'])
        order = self.order('Gallery Dept чорна S')
        self.assertIsNone(order['product_id'])
        self.assertEqual(db.mark_order_in_1c(order['id'],1)['inventory_status'], 'unmatched')
        self.assertEqual(self.order('Штани Gallery Dept чорні S','pants')['product_id'], pants['id'])

    def test_conflicting_variant_hints_never_pick_last_token(self):
        for i, raw in enumerate(('Gallery Dept S M чорна', 'Gallery Dept чорна біла S')):
            order = self.order(raw, str(i))
            result = db.mark_order_in_1c(order['id'], 1)
            self.assertEqual(result['inventory_status'], 'unmatched')
        self.assertTrue(all(v['on_hand'] == 5 for v in db.list_product_variants(self.product['id'])))

    def test_legacy_repair_changes_only_product_link_and_is_idempotent(self):
        old = db.resolve_product('Gallery dept чорна S')
        order = self.order('Gallery dept чорна S')
        result = db.mark_order_in_1c(order['id'], 1)
        self.assertEqual(result['deducted_quantity'], 1)
        with db._transaction() as session:
            session.execute('UPDATE orders SET product_id=? WHERE id=?', (old['id'],order['id']))
        before_stock = db.list_product_variants(self.product['id'])
        before_stats = db.get_sales_stats('all')
        preview = repair_product_links(order_ids=[order['id']])
        self.assertEqual(len(preview), 1)
        self.assertEqual(db.get_order(order['id'])['product_id'], old['id'])
        applied = repair_product_links(order_ids=[order['id']], apply=True)
        self.assertEqual(len(applied), 1)
        self.assertEqual(db.get_order(order['id'])['product_id'], self.product['id'])
        self.assertEqual(db.list_product_variants(self.product['id']), before_stock)
        self.assertEqual(db.get_sales_stats('all'), before_stats)
        self.assertEqual(repair_product_links(apply=True), [])
        self.assertTrue(db.mark_order_in_1c(order['id'],1)['already_recorded'])
        with db._transaction() as session:
            movements = session.one('SELECT COUNT(*) AS amount FROM inventory_movements WHERE order_id=?', (order['id'],))
        self.assertEqual(movements['amount'], 1)

    def test_edit_order_reuses_base_product(self):
        order = self.order('Gallery Dept чорні S')
        edited = db.edit_order_by_ttn(order['ttn'], {'product':'Футболка Gallery Dept біла M'}, 1)
        self.assertEqual(edited['product_id'], self.product['id'])
        self.assertEqual(len(db.list_products()), 1)
