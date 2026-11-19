"""Private parcel import, one-click payment and exact multi-item accounting."""

import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import business_db as business
import callbacks
import repliq_inbox as inbox
from fulfillment_queue import ParcelQueue
import test_inventory as inventory_tests

TTN = '20450000000001'


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.fixture = inventory_tests.InventoryDatabaseTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.bot = inventory_tests.FakeBot()
        self.queue = ParcelQueue(Path(self.fixture.temp_dir.name) / 'bridge.sqlite3')
        self.receiver = inbox.Receiver(self.bot, self.queue, owner=7, group=-1001, topic=10)
        self.addCleanup(patch.stopall)
        patch.object(inbox, '_RECEIVER', self.receiver).start()
        patch.object(inbox, 'is_owner', lambda user: user == 7).start()
        patch.object(callbacks, 'is_employee', lambda user: user == 7).start()
        patch.object(inbox, 'process_manual_ttn', return_value=(True, {}, False, 'ok')).start()
        patch.object(inbox, 'update_shipment').start()
        self.pants = business.create_inventory_product('Штани Suvene', ['M'], ['BLACK'])
        self.tee = business.create_inventory_product('Футболка Gallery Dept', ['L'], ['WHITE'])
        self.pv = business.list_product_variants(self.pants['id'])[0]
        self.tv = business.list_product_variants(self.tee['id'])[0]
        for product, variant, cost in ((self.pants, self.pv, '400'), (self.tee, self.tv, '200')):
            business.set_product_transport_cost(product['id'], 'SEA', cost, 7)
            business.receive_inventory_batch(variant['id'], 'SEA', 5, 7)
        self.data = {'version': 1, 'account': 'olx-account', 'thread': 'thread-a', 'buyer': 'buyer-a', 'advert': '101',
                     'invoice_id': 'ABC123', 'invoice_key': 'a' * 64, 'method': 'cod', 'total_cents': 210000,
                     'advance_cents': 10000, 'cod_cents': 200000,
                     'items': [{'product_id': self.pants['id'], 'name': self.pants['canonical_name'], 'size': 'M', 'color': 'BLACK', 'quantity': 2, 'unit_price_cents': 80000},
                               {'product_id': self.tee['id'], 'name': self.tee['canonical_name'], 'size': 'L', 'color': 'WHITE', 'quantity': 1, 'unit_price_cents': 50000}],
                     'recipient': {'name': 'Коваленко Олег Петрович', 'phone': '+380671234567', 'place': 'Львів, Львівська область', 'branch': 'Відділення №12'}}

    def created(self, data=None):
        row = self.queue.put(data or self.data)
        self.queue.claim_nova()
        self.queue.profile(row['id'], row['content_key'], 'first', 'test-profile')
        self.queue.before_write(row['id'], row['content_key'], 'creating')
        self.queue.carrier_done(row['id'], row['content_key'], TTN, 'ref')
        return self.queue.get(row['id'])

    def delivered(self):
        row = self.created()
        self.assertTrue(self.receiver.deliver())
        return self.queue.get(row['id'])

    def call(self, data, owner=7, group=-1001, message_id=1):
        return SimpleNamespace(id='callback', data=data, from_user=SimpleNamespace(id=owner, full_name='Owner'),
                               message=SimpleNamespace(message_id=message_id, chat=SimpleNamespace(id=group), message_thread_id=10,
                                                       text=self.bot.sent[0].text if self.bot.sent else '', reply_markup=None))

    def shipped(self):
        row = self.confirmed()
        callbacks.handle_callback(self.bot, self.call('sub:c:' + row['submission_id']))
        return self.queue.get(row['id'])

    def confirmed(self):
        row = self.delivered()
        self.queue.payment(row['id'], row['invoice_key'], 7, True)
        callbacks.handle_callback(self.bot, self.call('sub:r:' + row['submission_id']))
        return self.queue.get(row['id'])

    def test_import_publishes_one_card_and_does_not_mark_anything_shipped_or_sold(self):
        row = self.delivered()
        self.assertEqual(len(self.bot.sent), 1)

        self.assertIn(TTN, self.bot.sent[0].text)
        self.assertIn('Оплата ще не перевірена', self.bot.sent[0].text)
        self.assertIn('Передоплата: 100', self.bot.sent[0].text)
        self.assertIn('Разом за товари: 2100', self.bot.sent[0].text)
        self.assertIn('при отриманні: 2000', self.bot.sent[0].text)
        callbacks_data = [
            button.callback_data
            for buttons in self.bot.sent[0].reply_markup.keyboard
            for button in buttons
            if getattr(button, 'callback_data', None)
        ]
        self.assertNotIn('sub:r:' + row['submission_id'], callbacks_data)
        self.assertNotIn('sub:c:' + row['submission_id'], callbacks_data)
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['reserved'], 0)
        self.assertEqual(business.get_inventory_variant(self.tv['id'])['reserved'], 0)
        self.assertIsNone(business.get_order_by_ttn(TTN))
        self.receiver.next_send = 0
        self.assertFalse(self.receiver.deliver())
        self.assertEqual(len(self.bot.sent), 1)

    def test_monobank_tolerance_shows_actual_credit_without_changing_cod_or_confirming_shipment(self):
        import json
        row=self.created()
        with self.queue.db() as db:
            db.execute("UPDATE parcels SET paid=1,payment_source='monobank',bank_payment=? WHERE id=?",
                       (json.dumps({'source':'monobank','actual_cents':9400,'credited_cents':10000}),row['id']))
        self.assertTrue(self.receiver.deliver())
        row=self.queue.get(row['id'])
        text=self.bot.sent[0].text
        self.assertIn('mono: отримано 94 грн',text)
        self.assertIn('Зараховано як передоплату 100 грн',text)
        self.assertIn('при отриманні: 2000',text)
        self.assertNotIn('Перевір оплату та',text)
        self.assertEqual(row['ledgerly_status'],'confirmed')
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['reserved'],2)
        self.assertEqual(business.get_order_by_ttn(TTN)['status'], 'CONFIRMED')

    def test_owner_can_override_monobank_and_auto_does_not_reenable_it(self):
        row=self.delivered()
        self.queue.payment(row['id'],row['invoice_key'],7,False)
        changed=self.queue.get(row['id'])
        self.assertEqual(changed['payment_source'],'owner')
        self.assertEqual(changed['payment_manual_lock'],1)
        self.assertIn('Автоперевірка цього замовлення призупинена',inbox.card_text(changed))

    def test_full_payment_card_requests_manual_verification(self):
        data=copy.deepcopy(self.data)
        data.update(method='full',advance_cents=data['total_cents'],cod_cents=0,bank_payment={'scope':'old-marker'})
        row=self.created(data)
        text=inbox.card_text(row)
        self.assertIn('Перевір оплату —',text)
        self.assertNotIn('Дочекайся автоматичної перевірки',text)

    def test_bank_amendment_is_visible_as_review_needed(self):
        row=self.created()
        with self.queue.db() as db:
            db.execute("UPDATE parcels SET paid=0,payment_source='monobank_review' WHERE id=?",(row['id'],))
        self.assertIn('Банківська операція змінилася',inbox.card_text(self.queue.get(row['id'])))
    def test_payment_is_an_owner_only_single_click_without_a_customer_message(self):
        row = self.delivered()
        token = 'rp:%s:%s:paid' % (row['id'], row['invoice_key'][:12])
        callbacks.handle_callback(self.bot, self.call(token, owner=8))
        self.assertFalse(self.queue.get(row['id'])['paid'])
        callbacks.handle_callback(self.bot, self.call(token))
        self.assertTrue(self.queue.get(row['id'])['paid'])
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIsNone(business.get_order_by_ttn(TTN))

    def test_owner_can_request_receipt_for_unconfirmed_payment_without_duplicate_events(self):
        row=self.delivered()
        token='rp:%s:%s:unpaid' % (row['id'],row['invoice_key'][:12])
        self.assertTrue(any(button.callback_data==token for line in inbox.keyboard(row).keyboard for button in line))
        callbacks.handle_callback(self.bot,self.call(token,owner=8))
        self.assertEqual(self.queue.get(row['id'])['payment_review_revision'],0)
        callbacks.handle_callback(self.bot,self.call(token))
        callbacks.handle_callback(self.bot,self.call(token))
        changed=self.queue.get(row['id'])
        self.assertEqual(changed['payment_review_revision'],1)
        self.assertFalse(changed['paid'])
        self.assertTrue(changed['payment_manual_lock'])
        self.assertFalse(any(button.callback_data==token for line in inbox.keyboard(changed).keyboard for button in line))
        self.assertEqual(len(self.bot.sent),1)
        self.assertIn('Очікуємо повторну квитанцію',inbox.bank_payment_text(changed))
        self.queue.attach_receipts(row['id'],['https://example.test/repeated-receipt.jpg'])
        with_receipt=self.queue.get(row['id'])
        self.assertIn('Отримано нову квитанцію',inbox.bank_payment_text(with_receipt))
        signature=inbox.review_signature(with_receipt)
        callbacks.handle_callback(self.bot,self.call(token))
        reviewed_again=self.queue.get(row['id'])
        self.assertEqual(reviewed_again['payment_review_revision'],2)
        self.assertNotEqual(inbox.review_signature(reviewed_again),signature)

    def carrier_order(self):
        self.data.update(version=2, payment_policy='carrier_delivery', invoice_id='RQ-123456ABCDEF',
                         advance_cents=0, cod_cents=self.data['total_cents'])
        return self.delivered()

    def test_carrier_order_preserves_full_cod_and_displays_order_number(self):
        row = self.carrier_order()
        self.assertEqual(inbox.validate(row)['cod_cents'], 210000)
        text = self.bot.sent[0].text
        self.assertIn('RQ-123456ABCDEF', text)
        self.assertIn('при отриманні: 2100', text)
        self.assertIn('Передоплати за товар немає', text)
        self.assertNotIn('Передоплата: 100', text)
        self.assertIn('Замовлення №RQ-123456ABCDEF', inbox.raw_text(row))
        wrong = copy.deepcopy(row)
        wrong['data'].update(advance_cents=10000, cod_cents=200000)
        with self.assertRaises(ValueError):
            inbox.validate(wrong)

    def test_refresh_payment_button_cannot_attest_a_payment(self):
        row = self.carrier_order()
        with self.queue.db() as db:
            db.execute('UPDATE parcels SET delivery_next=9999999999 WHERE id=?', (row['id'],))
        token = 'rp:%s:%s:check' % (row['id'], row['invoice_key'][:12])
        callbacks.handle_callback(self.bot, self.call(token, owner=8))
        self.assertEqual(self.queue.get(row['id'])['delivery_next'], 9999999999)
        callbacks.handle_callback(self.bot, self.call(token))
        current = self.queue.get(row['id'])
        self.assertEqual(current['delivery_next'], 0)
        self.assertFalse(current['paid'])

    def test_owner_manual_delivery_attestation_has_a_distinct_source(self):
        row = self.carrier_order()
        token = 'rp:%s:%s:paid' % (row['id'], row['invoice_key'][:12])
        callbacks.handle_callback(self.bot, self.call(token, owner=8))
        self.assertFalse(self.queue.get(row['id'])['paid'])
        callbacks.handle_callback(self.bot, self.call(token))
        current = self.queue.get(row['id'])
        self.assertEqual(current['delivery_state'], 'manual')
        self.assertEqual(current['paid_by'], 7)
        self.assertIn('власником вручну', inbox.card_text(current))

    def test_reservation_waits_for_fresh_delivery_confirmation_and_expiry_blocks_dispatch(self):
        import time
        row = self.carrier_order()
        call = self.call('sub:r:' + row['submission_id'])
        callbacks.handle_callback(self.bot, call)
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['reserved'], 0)
        self.queue.delivery_result(row, {'state':'paid', 'reason':'nova_confirmed', 'cost_cents':9550, 'outstanding_cents':0})
        callbacks.handle_callback(self.bot, call)
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['reserved'], 2)
        with self.queue.db() as db:
            db.execute('UPDATE parcels SET delivery_checked=? WHERE id=?', (time.time()-181, row['id']))
        self.assertFalse(inbox.shipment_allowed(self.bot, self.call('sub:c:' + row['submission_id']), row['submission_id']))

    def test_shipped_card_retains_number_and_waybill_without_payment_buttons(self):
        row = self.carrier_order()
        self.queue.payment(row['id'], row['invoice_key'], 7, True)
        callbacks.handle_callback(self.bot, self.call('sub:r:' + row['submission_id']))
        callbacks.handle_callback(self.bot, self.call('sub:c:' + row['submission_id']))
        current = self.queue.get(row['id'])
        self.assertEqual(current['ledgerly_status'], 'shipped')
        text = inbox.card_text(current)
        self.assertIn('Відправлено', text)
        self.assertIn('RQ-123456ABCDEF', text)
        self.assertIn(TTN, text)
        self.assertEqual(inbox.keyboard(current).keyboard, [])
        self.assertTrue(any(r['id']==row['id'] for r in self.queue.notifications()))

    def test_notifications_after_two_hundred_old_cards_are_not_starved(self):
        for i in range(201):
            old = self.queue.put(dict(self.data, invoice_id='old-%s' % i))
            with self.queue.db() as db:
                db.execute("UPDATE parcels SET nova_status='created',ledgerly_status='sent',message_id=1,notified_revision=payment_revision WHERE id=?", (old['id'],))
        row = self.delivered()
        self.assertEqual([r['id'] for r in self.queue.notifications()], [row['id']])

    def test_wrong_group_message_or_old_invoice_key_cannot_mark_payment(self):
        row = self.delivered()
        token = 'rp:%s:%s:paid' % (row['id'], row['invoice_key'][:12])
        for call in (self.call(token, group=-1002), self.call(token, message_id=2), self.call('rp:%s:%s:paid' % (row['id'], 'b'*12))):
            inbox.callback_payment(self.bot, call)
        self.assertFalse(self.queue.get(row['id'])['paid'])

    def test_shipping_before_payment_check_is_blocked(self):
        row = self.delivered()
        callbacks.handle_callback(self.bot, self.call('sub:r:' + row['submission_id']))
        self.assertIsNone(business.get_order_by_ttn(TTN))
        self.assertEqual(business.get_submission(row['submission_id'])['status'], 'PENDING')

    def test_payment_then_refresh_reserves_automatically_without_order_confirmation(self):
        row = self.delivered()
        callbacks.handle_callback(self.bot, self.call('rp:%s:%s:paid' % (row['id'], row['invoice_key'][:12])))
        self.receiver.next_send = 0
        self.receiver.refresh()
        current = self.queue.get(row['id'])
        self.assertEqual(current['ledgerly_status'], 'confirmed')
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['reserved'], 2)
        buttons = [b.callback_data for line in inbox.keyboard(current).keyboard for b in line if b.callback_data]
        self.assertIn('sub:c:' + row['submission_id'], buttons)
        self.assertFalse(any(b.startswith('sub:r:') for b in buttons))
        self.receiver.next_send = 0
        self.receiver.refresh()
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['reserved'], 2)
        callbacks.handle_callback(self.bot, self.call('sub:c:' + row['submission_id']))
        self.assertEqual(business.get_order_by_ttn(TTN)['status'], 'SHIPPED')

    def test_confirmation_reserves_every_item_before_shipping_without_double_reserve(self):
        row = self.confirmed()
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['reserved'], 2)
        self.assertEqual(business.get_inventory_variant(self.tv['id'])['reserved'], 1)
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['on_hand'], 5)
        order = business.get_order_by_ttn(TTN)
        self.assertEqual(order['status'], 'CONFIRMED')
        self.assertEqual(row['ledgerly_status'], 'confirmed')
        pants = business.get_inventory_variant(self.pv['id'])
        self.assertEqual(pants['awaiting_shipment'], 2)
        self.assertEqual(pants['in_transit'], 0)

        callbacks.handle_callback(self.bot, self.call('sub:c:' + row['submission_id']))
        order = business.get_order_by_ttn(TTN)
        self.assertEqual(order['status'], 'SHIPPED')
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['reserved'], 2)
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['awaiting_shipment'], 0)
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['in_transit'], 2)
        self.assertEqual(order['quantity'], 3)
        self.assertEqual(order['sale_price_cents'], 210000)
        self.assertEqual(len(business.list_inventory_products()), 2)

    def test_cancelling_confirmed_order_releases_every_reserved_item(self):
        row = self.confirmed()
        callbacks.handle_callback(self.bot, self.call('sub:x:' + row['submission_id']))
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['reserved'], 0)
        self.assertEqual(business.get_inventory_variant(self.tv['id'])['reserved'], 0)
        self.assertEqual(business.get_order_by_ttn(TTN)['status'], 'CANCELLED')
        self.assertEqual(self.queue.get(row['id'])['ledgerly_status'], 'cancelled')

    def test_multi_item_fifo_cost_and_profit_are_counted_once(self):
        self.shipped()
        order = business.get_order_by_ttn(TTN)
        result = business.mark_order_in_1c(order['id'], 7, 'Owner')
        self.assertEqual(result['deducted_quantity'], 3)
        self.assertEqual(result['purchase_cost_cents'], 100000)
        self.assertEqual(result['profit_cents'], 110000)
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['on_hand'], 3)
        self.assertEqual(business.get_inventory_variant(self.tv['id'])['on_hand'], 4)
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['reserved'], 0)
        self.assertTrue(business.mark_order_in_1c(order['id'], 7)['already_recorded'])
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['on_hand'], 3)
        with business._transaction() as session:
            self.assertEqual(session.one('SELECT COUNT(*) AS n FROM orders')['n'], 1)
            self.assertEqual(session.one('SELECT SUM(total_cost_cents) AS cost FROM inventory_batch_allocations')['cost'], 100000)

    def test_return_releases_all_items_without_decreasing_on_hand(self):
        self.shipped()
        business.update_order_status_by_ttn(TTN, 'RETURNED', actor_user_id=7, actor_name='Owner')
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['reserved'], 0)
        self.assertEqual(business.get_inventory_variant(self.tv['id'])['reserved'], 0)
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['on_hand'], 5)

    def test_shortage_does_not_partially_reserve_a_bundle(self):
        row = self.delivered()
        business.change_inventory_stock(self.tv['id'], -5, 7)
        self.queue.payment(row['id'], row['invoice_key'], 7, True)
        callbacks.handle_callback(self.bot, self.call('sub:r:' + row['submission_id']))
        self.assertEqual(business.get_inventory_variant(self.pv['id'])['reserved'], 0)
        self.assertEqual(business.get_inventory_variant(self.tv['id'])['reserved'], 0)

    def test_gross_sale_value_is_not_replaced_by_cod_remainder(self):
        self.shipped()
        self.assertEqual(business.get_order_by_ttn(TTN)['sale_price_cents'], 210000)

    def test_full_payment_import_has_zero_due_on_receipt(self):
        data = dict(self.data, method='full', advance_cents=210000, cod_cents=0)
        row = self.created(data)
        self.assertTrue(self.receiver.deliver())
        self.assertIn('при отриманні: 0 грн', self.bot.sent[0].text)
        self.assertEqual(self.queue.get(row['id'])['data']['method'], 'full')

    def test_manifest_id_or_price_mismatch_is_rejected_before_publishing(self):
        data = copy.deepcopy(self.data)
        data['items'][1]['product_id'] = 'wrong-product'
        self.created(data)
        self.assertFalse(self.receiver.deliver())
        self.assertEqual(self.bot.sent, [])

    def test_unknown_telegram_result_does_not_create_a_duplicate_card(self):
        row = self.created()
        with patch.object(inbox, 'attach_submission_message', side_effect=RuntimeError('test crash')):
            self.assertFalse(self.receiver.deliver())
        self.assertEqual(len(self.bot.sent), 1)
        self.queue.recover_ledgerly()
        self.receiver.next_send = 0
        self.assertFalse(self.receiver.deliver())
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(self.queue.get(row['id'])['ledgerly_status'], 'uncertain')

    def test_deterministic_submission_id_is_idempotent_but_not_overwritable(self):
        identity = 'a'*16
        first = business.create_submission('Test', {'quantity': '1'}, [TTN], 7, 'Owner', submission_id=identity)
        second = business.create_submission('Test', {'quantity': '1'}, [TTN], 7, 'Owner', submission_id=identity)
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            business.create_submission('Changed customer', {'quantity': '1'}, [TTN], 7, 'Owner', submission_id=identity)

    def test_mutated_total_cannot_be_imported(self):
        data = dict(self.data, total_cents=200000)
        self.created(data)
        self.assertFalse(self.receiver.deliver())
        self.assertEqual(self.bot.sent, [])


if __name__ == '__main__':
    unittest.main()
