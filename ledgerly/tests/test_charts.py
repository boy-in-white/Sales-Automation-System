import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import business_db as db
from chart_data import dashboard_data
from charts import render_dashboard
import handlers
import callbacks


class ChartsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_url = db.BUSINESS_DATABASE_URL
        db.BUSINESS_DATABASE_URL = 'sqlite:///' + self.temp.name + '/charts.sqlite3'
        self.addCleanup(setattr, db, 'BUSINESS_DATABASE_URL', self.old_url)
        db.init_business_db()

    def sale(self, ttn, price='1000', cost='400'):
        return db.record_order(ttn=ttn, product_name='Штани Suvene', sale_type_label='ОЛХ',
                               carrier='Offline', sale_price=price, purchase_cost=cost, initial_status='DELIVERED')

    def test_aggregates_match_statistics_and_keep_unpriced_profit_unknown(self):
        self.sale('priced')
        self.sale('unpriced', cost='')
        result = dashboard_data()
        self.assertEqual(result['stats']['turnover_cents'], 200000)
        self.assertEqual(sum(row['turnover_cents'] for row in result['timeline']), 200000)
        self.assertEqual(sum(row['profit_cents'] for row in result['timeline']), 60000)
        self.assertEqual(result['stats']['cost_pending_count'], 1)
        self.assertEqual(sum(r['turnover_cents'] for r in result['top_products']), 200000)

    def test_refusals_remain_real_after_month_reset(self):
        self.sale('refused')
        db.update_order_status_by_ttn('refused', 'REFUSED')
        db.clear_current_month_analytics(1, 'Owner')
        result = dashboard_data()
        self.assertEqual(result['refusals'], 1)
        self.assertEqual(result['stats']['turnover_cents'], 0)

    def test_multi_item_parcel_revenue_is_split_by_actual_product(self):
        self.sale('multi', price='1500')
        second = db.create_inventory_product('Футболка Gallery Dept', ['M'], ['BLACK'])
        first_product = db.create_inventory_product('Штани Suvene', ['M'], ['BLACK'])
        variants = [db.list_product_variants(p['id'])[0] for p in (first_product, second)]
        with db._transaction() as session:
            for i, (product, variant, price) in enumerate(zip((first_product,second), variants, (100000,50000))):
                session.execute('INSERT INTO repliq_parcel_items(ttn,line,product_id,variant_id,quantity,unit_price_cents) VALUES(?,?,?,?,1,?)',
                                ('multi', i, product['id'], variant['id'], price))
        products = dashboard_data('all')['top_products']
        self.assertEqual(sorted(int(r['turnover_cents']) for r in products), [50000,100000])

    def test_empty_and_loss_dashboards_render_valid_png(self):
        for mode in ('empty', 'loss'):
            if mode == 'loss': self.sale('loss', cost='1500')
            with render_dashboard(dashboard_data('all')) as output:
                self.assertTrue(output.read(8).startswith(b'\x89PNG'))
                self.assertLess(len(output.getvalue()), 5_000_000)

    def test_invalid_periods_fail_before_query(self):
        for period in ('2026-13', '0000-01', '9999-12', 'month;DROP TABLE orders'):
            with self.assertRaises(ValueError): dashboard_data(period)

    def test_product_names_cannot_break_rendering_with_math_markup(self):
        self.sale('markup')
        data = dashboard_data()
        data['top_products'][0]['name'] = r'Штани $\unknown{broken}$'
        with render_dashboard(data) as output:
            self.assertTrue(output.read(8).startswith(b'\x89PNG'))

    def test_chart_command_never_queries_data_while_locked(self):
        bot = SimpleNamespace(send_message=lambda *a,**k: None)
        message = SimpleNamespace(text='/charts', caption=None, chat=SimpleNamespace(id=123),
                                  from_user=SimpleNamespace(id=123))
        with patch.object(handlers, 'is_private_user_allowed', return_value=True), \
             patch.object(handlers, 'is_owner', return_value=True), \
             patch.object(handlers, 'ANALYTICS_ACCESS', SimpleNamespace(is_unlocked=lambda _:False)), \
             patch('chart_data.dashboard_data') as query:
            handlers.handle_private_message(bot, message)
            query.assert_not_called()

    def test_forwarded_financial_button_does_not_send_to_group(self):
        call = SimpleNamespace(id='test', data='analytics:chart:all', from_user=SimpleNamespace(id=123),
                               message=SimpleNamespace(chat=SimpleNamespace(id=-1001,type='supergroup')))
        bot = SimpleNamespace(answer_callback_query=lambda *a,**kw: None)
        with patch.object(callbacks, 'is_owner', return_value=True), \
             patch.object(callbacks, 'ANALYTICS_ACCESS', SimpleNamespace(is_unlocked=lambda _:True)), \
             patch('handlers._handle_charts') as render:
            callbacks.handle_callback(bot, call)
            render.assert_not_called()
