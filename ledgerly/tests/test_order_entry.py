"""Exercise durable manual entry through real callbacks and SQL accounting."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import business_db as db
import callbacks
import order_entry as entry
import tracking_db
import test_inventory as inventory_tests


class ManualOrderTests(unittest.TestCase):
    def setUp(self):
        fixture = inventory_tests.InventoryDatabaseTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.addCleanup(patch.stopall)
        patch.object(tracking_db, 'DATABASE_URL', db.BUSINESS_DATABASE_URL).start()
        tracking_db.init_db()
        patch.object(callbacks, 'is_admin', lambda user: user == 7).start()
        patch.object(callbacks, 'is_employee', lambda user: user == 7).start()
        patch('incoming_group_listener.process_manual_ttn', return_value=(True, {}, False, 'ok')).start()
        patch('repliq_inbox._RECEIVER', None).start()
        self.bot = inventory_tests.FakeBot()
        self.user = SimpleNamespace(id=7, full_name='Owner')
        self.product = db.create_inventory_product('Футболки Gallery Dept', ['S', 'M'], ['BLACK', 'WHITE'])
        self.variant = next(v for v in db.list_product_variants(self.product['id']) if v['size_code'] == 'S' and v['color_code'] == 'BLACK')
        db.set_product_transport_cost(self.product['id'], 'AIR', '360', 7)
        db.receive_inventory_batch(self.variant['id'], 'AIR', 5, 7)

    def press(self, action, value='x', *, data=None, user=None, chat=7):
        wizard = db.get_inventory_wizard(7)
        payload = wizard['payload'] if wizard else {}
        call = SimpleNamespace(id='tap', from_user=user or self.user,
            data=data or 'new:%s:%s:%s:%s' % (payload['token'], payload['revision'], action, value),
            message=SimpleNamespace(chat=SimpleNamespace(id=chat, type='private'), message_id=1, text=''))
        callbacks.handle_callback(self.bot, call)
        return call

    def text(self, text):
        return entry.handle_text(self.bot, SimpleNamespace(from_user=self.user, chat=SimpleNamespace(id=7), text=text))

    def review(self, kind='cod', ttn='20450000000011'):
        entry.start_order(self.bot, 7, 7)
        self.press('kind', kind)
        self.press('product', self.product['id'])
        self.press('color', 'BLACK')
        self.press('size', self.variant['id'])
        self.press('qty', '2')
        self.text('2000,50')
        if kind != 'offline':
            self.text(ttn)
        self.assertEqual(db.get_inventory_wizard(7)['step'], 'ORDER_REVIEW')
        return db.get_inventory_wizard(7)['payload']['token']

    def test_save_reserves_then_single_shipping_button_and_onec_deduct_exactly_once(self):
        token = self.review()
        call = self.press('save')
        callbacks.handle_callback(self.bot, call)  # Duplicate network delivery / double tap.
        self.assertEqual(db.business_health()['orders'], 1)
        self.assertEqual(len(db.list_inventory_products()), 1)
        saved = db.get_submission(token)
        self.assertEqual(saved['status'], 'CONFIRMED')
        order = db.get_order_by_ttn('20450000000011')
        self.assertEqual(order['product_id'], self.product['id'])
        self.assertEqual((order['quantity'], order['sale_price_cents'], order['status']), (2, 200050, 'CONFIRMED'))
        stock = db.get_inventory_variant(self.variant['id'])
        self.assertEqual((stock['on_hand'], stock['reserved'], stock['in_transit']), (5, 2, 0))
        cards = [m for m in self.bot.sent if m.reply_markup and any(b.callback_data == 'sub:c:' + token for row in m.reply_markup.keyboard for b in row)]
        self.assertEqual(len(cards), 1)
        self.assertFalse(any(b.callback_data.startswith('sub:r:') for row in cards[0].reply_markup.keyboard for b in row))
        ship = SimpleNamespace(id='ship', data='sub:c:' + token, from_user=self.user, message=cards[0])
        callbacks.handle_callback(self.bot, ship)
        callbacks.handle_callback(self.bot, ship)
        self.assertEqual(db.get_inventory_variant(self.variant['id'])['in_transit'], 2)
        done = db.mark_order_in_1c(order['id'], 7)
        self.assertEqual((done['deducted_quantity'], done['purchase_cost_cents'], done['profit_cents']), (2, 72000, 128050))
        self.assertTrue(db.mark_order_in_1c(order['id'], 7)['already_recorded'])
        self.assertEqual(db.get_inventory_variant(self.variant['id'])['on_hand'], 3)

    def test_stale_buttons_cannot_change_new_form_or_run_another_users_form(self):
        entry.start_order(self.bot, 7, 7)
        old = self.press('kind', 'olx')
        self.press('product', self.product['id'])
        self.press('color', 'WHITE', user=SimpleNamespace(id=8, full_name='Other'))
        self.assertEqual(db.get_inventory_wizard(7)['step'], 'ORDER_COLOR')
        callbacks.handle_callback(self.bot, old)
        self.assertEqual(db.get_inventory_wizard(7)['step'], 'ORDER_COLOR')
        self.press('color', 'WHITE', chat=-100)
        self.assertEqual(db.get_inventory_wizard(7)['step'], 'ORDER_COLOR')
        entry.start_order(self.bot, 7, 7)
        callbacks.handle_callback(self.bot, old)
        self.assertEqual(db.get_inventory_wizard(7)['step'], 'ORDER_TYPE')

    def test_invalid_price_quantity_and_ttn_leave_current_step_unchanged(self):
        entry.start_order(self.bot, 7, 7)
        self.press('kind', 'fop'); self.press('product', self.product['id'])
        self.press('color', 'BLACK'); self.press('size', self.variant['id'])
        for value in ('0', '-1', '1.5', '1000'):
            self.text(value)
            self.assertEqual(db.get_inventory_wizard(7)['step'], 'ORDER_QUANTITY')
        self.text('2')
        for value in ('0', '-5', '100.123', 'nan', '1e5', '2+3'):
            self.text(value)
            self.assertEqual(db.get_inventory_wizard(7)['step'], 'ORDER_PRICE')
        self.text('100')
        for value in ('123', '20450000000011 20450000000012', '123/../../'):
            self.text(value)
            self.assertEqual(db.get_inventory_wizard(7)['step'], 'ORDER_TTN')
        self.text('20 4500 0000 0011')
        self.assertEqual(db.get_inventory_wizard(7)['step'], 'ORDER_REVIEW')

    def test_duplicate_ttn_cannot_create_second_submission(self):
        token = self.review()
        self.press('save')
        self.review(ttn='20450000000012')
        # A second process may add the TTN while the owner reads the review.
        db.create_submission('other', {}, ['20450000000012'], 99, 'Other')
        self.press('save')
        self.assertEqual(db.get_inventory_wizard(7)['step'], 'ORDER_REVIEW')
        self.assertEqual(db.business_health()['orders'], 1)
        self.assertEqual(db.get_submission(token)['ttns'], ['20450000000011'])

    def test_back_cancel_and_offline_do_not_create_early_sales(self):
        self.review(kind='offline')
        self.press('back')
        self.assertEqual(db.get_inventory_wizard(7)['step'], 'ORDER_PRICE')
        self.text('1200')
        self.press('cancel')
        self.assertEqual(db.business_health()['orders'], 0)
        token = self.review(kind='offline')
        self.press('save')
        self.assertEqual(db.business_health()['orders'], 0)
        saved = db.get_submission(token)
        self.assertTrue(saved['parsed']['offline'])
        self.assertEqual(saved['ttns'], [])

    def test_deleted_variant_is_not_saved_and_shortage_never_makes_negative_stock(self):
        self.review()
        with db._transaction() as session:
            session.execute('UPDATE product_variants SET active=0 WHERE id=?', (self.variant['id'],))
        self.press('save')
        self.assertEqual(db.business_health()['orders'], 0)
        with db._transaction() as session:
            session.execute('UPDATE product_variants SET active=1 WHERE id=?', (self.variant['id'],))
        db.change_inventory_stock(self.variant['id'], -5, 7)
        self.press('save')
        order = db.get_order_by_ttn('20450000000011')
        self.assertIsNotNone(order)
        stock = db.get_inventory_variant(self.variant['id'])
        self.assertEqual((stock['on_hand'], stock['reserved']), (0, 0))
        self.assertTrue(any('Резерв складу потребує перевірки' in m.text for m in self.bot.sent))

    def test_concurrent_saves_publish_one_card(self):
        token = self.review()
        payload = db.get_inventory_wizard(7)['payload']
        data = 'new:%s:%s:save:x' % (token, payload['revision'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.press('save', data=data), range(2)))
        self.assertEqual(len(results), 2)
        self.assertEqual(db.business_health()['orders'], 1)
        self.assertEqual(db.get_inventory_variant(self.variant['id'])['reserved'], 2)

    def test_persisted_form_resumes_and_processing_crash_can_be_retried(self):
        token = self.review()
        entry.render(self.bot, 7)
        self.assertIn('Перевір замовлення', self.bot.sent[-1].text)
        self.press('save')
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with db._transaction() as session:
            session.execute("UPDATE order_submissions SET status='PROCESSING',updated_at=? WHERE id=?", (old, token))
        from submission_service import prepare_submission
        self.assertTrue(prepare_submission(self.bot, db.get_submission(token), 7)[0])
        self.assertEqual(db.get_inventory_variant(self.variant['id'])['reserved'], 2)

    def test_uncertain_telegram_send_does_not_publish_duplicate_card(self):
        token = self.review()
        original = self.bot.send_message
        def unavailable(chat_id, text, **kwargs):
            if kwargs.get('reply_markup') and any(b.callback_data.startswith('sub:') for row in kwargs['reply_markup'].keyboard for b in row):
                raise TimeoutError('Unknown send result')
            return original(chat_id, text, **kwargs)
        self.bot.send_message = unavailable
        self.press('save')
        self.assertEqual(db.get_submission(token)['telegram_message_id'], -1)
        self.assertFalse(entry.publish_submission(self.bot, token))
        self.assertEqual(db.business_health()['orders'], 1)
