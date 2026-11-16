"""Delivery-only checkout: fake carrier/OLX, real queues, no external writes."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_consultation_flow as consultation
import test_fulfillment as legacy
import test_olx_delivery_live_flow as worker_fixture
from carrier_payment import POLICY, evidence, ready, tracking_url
from checkout import Checkout
from fulfillment import Fulfillment, parcel_payload
from nova_shipping import NovaShipping, contents_description
from order_summary import invoice_key, snapshot


class Carrier(legacy.Api):
    def __init__(self):
        super().__init__()
        self.payment = 'NeedPayment'
        self.code = '1'
        self.fail_tracking = False
        self.by_number = {}

    def request(self, model, method, props, write):
        if method != 'getStatusDocuments':
            return super().request(model, method, props, write)
        self.calls.append((model, method, props, write))
        if self.fail_tracking:
            raise OSError('temporary outage')
        return {'success': True, 'data': [dict(Number=d['DocumentNumber'], PhoneRecipient=d['Phone'],
                StatusCode=self.code, PayerType='Recipient', DocumentCost='95.50',
                ExpressWaybillPaymentStatus=self.by_number.get(d['DocumentNumber'], self.payment),
                ExpressWaybillAmountToPay='0' if self.by_number.get(d['DocumentNumber'], self.payment)=='Payed' else '895.50') for d in props['Documents']]}


class DeliveryCheckout(unittest.TestCase):
    def setUp(self):
        self.s = consultation.ConsultationTests()
        self.s.setUp()
        self.s.catalog.shop['cod_payment'] = POLICY
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'shipping-settings.json').write_text(json.dumps(legacy.settings(self.root)))
        self.api = Carrier()
        self.service = Fulfillment(self.root, threading.Event(), self.s.catalog,
                                  carrier_factory=lambda p: NovaShipping(p, self.api.request), inventory=self.s.stock)
        self.service.queue.announce_consumer(2)
        self.calendar = patch('fulfillment.active_profile', return_value='first')
        self.calendar.start()
        self.addCleanup(self.calendar.stop)

    def order(self):
        self.s.order()
        plan = self.s.turn('2')
        self.assertNotIn('100 грн', plan.text)
        self.assertNotIn(consultation.TEST_CARD, plan.text)
        self.s.context['fulfillment_opt_in'] = True
        self.s.turn(legacy.ADDRESS)
        self.s.turn('так, усе правильно')
        return parcel_payload(self.s.catalog, self.s.context, 'account', 'thread', 'buyer', '101')

    def created(self):
        payload = self.order()
        row = self.service.queue.put(payload)
        self.assertTrue(self.service.process_one())
        return self.service.queue.get(row['id'])

    def poll(self, row):
        self.service.queue.request_delivery_check(row['id'], row['invoice_key'])
        self.service.next_delivery_scan = 0
        self.service.process_delivery_payments()
        return self.service.queue.get(row['id'])

    def test_full_customer_journey_has_stable_number_no_deposit_and_full_cod(self):
        payload = self.order()
        self.assertIsNotNone(payload)
        self.assertRegex(payload['invoice_id'], r'^RQ-[A-F0-9]{12}$')
        self.assertEqual((payload['version'], payload['advance_cents'], payload['cod_cents']), (2, 0, 80000))
        self.assertEqual(payload['payment_policy'], POLICY)
        old = payload['invoice_id']
        self.s.turn('так')
        self.assertEqual(self.s.context['invoice_id'], old)
        row = self.service.queue.put(payload)
        self.service.process_one()
        props = self.api.saves()[0]
        self.assertEqual(props['BackwardDeliveryData'][0]['RedeliveryString'], '800')
        self.assertEqual(props['Description'], 'Штани, M, чорний')
        self.assertEqual(props['InfoRegClientBarcodes'], old)
        self.assertEqual(self.service.queue.put(payload)['id'], row['id'])
        self.assertFalse(self.service.process_one())

    def test_new_cod_does_not_require_bank_details(self):
        self.s.bot.merchant = {}
        self.order()
        self.assertFalse(self.s.context.get('payment_verified'))

    def test_payment_report_or_photo_is_never_proof(self):
        self.order()
        for text, attachment in [('Я оплатив доставку', False), ('Ось квитанція', True)]:
            self.s.turn(text, attachment=attachment)
            self.assertFalse(self.s.context.get('payment_verified'))
            self.assertFalse(self.s.context.get('receipt_submitted'))
            self.assertFalse(self.s.context.get('payment_reported'))

    def test_receipt_is_not_requested_and_unavailable_payment_gets_help(self):
        self.order()
        reply = self.s.turn('Квитанцію потрібно скидати?')
        self.assertIn('Квитанцію надсилати не потрібно', reply.text)
        self.assertNotEqual(self.s.context.get('awaiting'), 'payment_receipt')
        for text in ('Там неможливо оплатити доставку', 'Кнопки нема', 'Там тільки всю суму разом можна сплатити'):
            with self.subTest(text=text):
                reply = self.s.turn(text)
                self.assertFalse(reply.on_send.get('handoff_now'))
                self.assertFalse(self.s.context.get('payment_verified'))

    def test_ambiguous_partial_payment_does_not_release_order(self):
        row = self.poll(self.created())
        self.assertEqual(row['delivery_state'], 'waiting')
        self.assertFalse(ready(row))
        self.assertFalse(row['paid'])

    def test_explicit_carrier_confirmation_releases_only_the_matching_waybill(self):
        row = self.created()
        self.api.payment = 'Payed'
        result = self.poll(row)
        self.assertTrue(ready(result))
        self.assertIsNone(result['paid_by'])
        self.assertEqual(result['delivery_cost_cents'], 9550)
        bad = {'Number': '20450000000002', 'ExpressWaybillPaymentStatus': 'Payed', 'ExpressWaybillAmountToPay': '0'}
        self.assertEqual(evidence(bad, row)['state'], 'unknown')
        self.assertFalse(any(write for model, method, props, write in self.api.calls if method=='getStatusDocuments'))

    def test_refund_cancel_and_missing_payment_fields_cannot_be_paid(self):
        row = self.created()
        base = dict(Number=row['ttn'], PayerType='Recipient', StatusCode='1', DocumentCost='95.5',
                    ExpressWaybillPaymentStatus='Payed', ExpressWaybillAmountToPay='0')
        for changes in ({'StatusCode':'102'}, {'StatusCode':'11'}, {'DocumentCost':'0'}, {'PayerType':'Sender'},
                        {'ExpressWaybillPaymentStatus':'NeedPayment'}, {'ExpressWaybillAmountToPay':''},
                        {'PhoneRecipient':'380501111111'}, {'RefEW':'different-reference'}):
            with self.subTest(changes=changes):
                self.assertNotEqual(evidence(dict(base, **changes), row)['state'], 'paid')
        self.api.payment = 'Payed'
        row = self.poll(row)
        self.api.payment = 'NeedPayment'
        row = self.poll(row)
        self.assertEqual(row['delivery_state'], 'revoked')
        self.assertFalse(ready(row))

    def test_outage_does_not_extend_old_confirmation_or_create_false_payment(self):
        row = self.created()
        self.api.fail_tracking = True
        self.assertFalse(ready(self.poll(row)))
        self.api.fail_tracking = False
        self.api.payment = 'Payed'
        row = self.poll(row)
        self.assertTrue(ready(row))
        self.api.fail_tracking = True
        with self.service.queue.db() as db:
            db.execute('UPDATE parcels SET delivery_checked=? WHERE id=?', (time.time()-181, row['id']))
        row = self.poll(row)
        self.assertFalse(ready(row))
        self.assertEqual(row['delivery_state'], 'revoked')

    def test_manual_attestation_is_explicit_and_bound_to_the_order(self):
        row = self.created()
        self.assertFalse(self.service.queue.payment(row['id'], row['invoice_key'], 0, True))
        self.assertTrue(self.service.queue.payment(row['id'], row['invoice_key'], 7, True))
        row = self.service.queue.get(row['id'])
        self.assertTrue(ready(row))
        self.assertEqual(row['delivery_state'], 'manual')
        self.assertTrue(ready(row, now=row['delivery_checked']+181))
        self.assertFalse(ready(dict(row, nova_status='needs_review')))

    def test_description_contains_only_goods_size_and_color(self):
        self.assertEqual(contents_description([{'name':'Футболка Gallery Dept','size':'L','color':'WHITE','quantity':2}]), 'Футболка, L, білий')
        self.assertNotIn('100', self.s.turn('Для чого передоплата 100 грн?').text.replace('100 грн не потрібно',''))
        self.assertEqual(tracking_url(legacy.TTN), 'https://tracking.novaposhta.ua/#/uk/'+legacy.TTN)

    def test_other_customer_and_amended_waybill_cannot_inherit_payment(self):
        row = self.created()
        other = copy.deepcopy(row['data'])
        other.update(thread='another-thread', buyer='another-buyer', invoice_id='RQ-ABCDEF987654', invoice_key='b'*64)
        second = self.service.queue.put(other)
        self.service.queue.claim_nova()
        self.service.queue.profile(second['id'], second['content_key'], 'first', row['nova_profile_key'], row['nova_api_fingerprint'])
        self.service.queue.before_write(second['id'], second['content_key'], 'creating')
        self.service.queue.carrier_done(second['id'], second['content_key'], '20450000000002', 'second-ref')
        self.api.by_number[row['ttn']] = 'Payed'
        self.poll(row)
        self.assertTrue(ready(self.service.queue.get(row['id'])))
        self.assertFalse(ready(self.service.queue.get(second['id'])))
        changed = copy.deepcopy(row['data'])
        changed['recipient']['branch'] = 'Відділення №55'
        amended = self.service.queue.put(changed)
        self.assertEqual(amended['nova_status'], 'needs_review')
        self.assertFalse(ready(amended))

    def test_changed_api_profile_never_checks_a_waybill_under_another_key(self):
        row = self.created()
        config = legacy.settings(self.root)
        config['profiles']['first']['nova_api_key'] = 'unrelated-account'
        (self.root/'shipping-settings.json').write_text(json.dumps(config))
        row = self.poll(row)
        self.assertFalse(ready(row))
        self.assertEqual(row['delivery_reason'], 'original_profile_unavailable')
        self.assertFalse(any(method=='getStatusDocuments' for model, method, props, write in self.api.calls))

    def test_new_waybill_waits_for_current_ledgerly_without_affecting_legacy(self):
        payload = self.order()
        row = self.service.queue.put(payload)
        with self.service.queue.db() as db:
            db.execute('DELETE FROM bridge_consumers')
        self.assertFalse(self.service.process_one())
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.service.queue.get(row['id'])['nova_status'], 'pending')
        self.service.queue.announce_consumer(2)
        self.assertTrue(self.service.process_one())
        self.assertEqual(self.service.queue.get(row['id'])['nova_status'], 'created')

    def test_legacy_invoices_keep_their_original_amount_and_key(self):
        self.s.catalog.shop.pop('cod_payment')
        self.s.order(); self.s.turn('2')
        old = invoice_key(snapshot(self.s.catalog, self.s.context))
        self.s.catalog.shop['cod_payment'] = POLICY
        self.s.turn('Реквізити ще раз')
        self.assertEqual(self.s.context['checkout_amount'], '100')
        self.assertEqual(invoice_key(snapshot(self.s.catalog, self.s.context)), old)


class WorkerDelivery(unittest.TestCase):
    def setUp(self):
        self.f = worker_fixture.DeliveryFlow()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.catalog.shop['cod_payment'] = POLICY
        self.f.shop.write_text(json.dumps(self.f.catalog.data))
        from catalog import Catalog
        self.f.catalog = Catalog(self.f.catalog.data)
        self.f.worker.catalog = self.f.worker.base_catalog = self.f.catalog
        self.f.worker.dialogue.catalog = self.f.catalog
        root = self.f.store.path.parent
        (root/'shipping-settings.json').write_text(json.dumps(legacy.settings(root)))
        self.api = Carrier()
        self.service = Fulfillment(root, self.f.worker.stop, self.f.catalog,
                                  carrier_factory=lambda p: NovaShipping(p, self.api.request),
                                  inventory=self.f.worker.inventory, can_create=self.f.worker.parcel_can_create)
        self.f.worker.fulfillment = self.service
        self.service.queue.announce_consumer(2)
        self.calendar = patch('fulfillment.active_profile', return_value='first')
        self.calendar.start(); self.addCleanup(self.calendar.stop)

    def test_waybill_is_sent_before_payment_to_correct_buyer_with_order_number(self):
        self.f.process('2')
        self.f.process(legacy.ADDRESS)
        self.f.process('так, все правильно')
        self.assertTrue(self.service.process_one())
        self.f.worker.parcel_updates()
        context = self.f.context()
        self.assertFalse(context.get('payment_verified'))
        # The first pass refreshes the OLX counter after our preceding reply.
        self.f.worker.send_customer_update()
        self.f.worker.send_customer_update()
        recipient, body = self.f.client.sent[-1]
        self.assertEqual(recipient, 't1')
        self.assertIn('Ось ваша ТТН: '+legacy.TTN, body)
        self.assertIn(context['invoice_id'], body)
        self.assertIn('https://tracking.novaposhta.ua/', body)
        before = len(self.f.client.sent)
        self.f.worker.send_customer_update()
        self.assertEqual(len(self.f.client.sent), before)
        self.assertEqual(self.f.context('t2').get('invoice_id'), None)
        self.api.payment = 'Payed'
        self.service.process_delivery_payments()
        self.f.worker.parcel_updates()
        self.f.worker.send_customer_update()
        self.assertIn('оплату доставки підтверджено', self.f.client.sent[-1][1])
        self.assertIn(context['invoice_id'], self.f.client.sent[-1][1])
        before = len(self.f.client.sent)
        self.f.worker.send_customer_update()
        self.assertEqual(len(self.f.client.sent), before)

    def test_restart_recovers_unpaid_waybill_notification(self):
        from customer_updates import CustomerUpdates
        self.f.process('2'); self.f.process(legacy.ADDRESS); self.f.process('так')
        self.service.process_one(); self.f.worker.parcel_updates()
        with self.f.store.db() as db:
            db.execute('DELETE FROM customer_updates')
        self.f.worker.customer_updates = CustomerUpdates(self.f.store)
        self.f.worker.send_customer_update(); self.f.worker.send_customer_update()
        self.assertIn('Ось ваша ТТН: '+legacy.TTN, self.f.client.sent[-1][1])
        self.assertFalse(self.f.context().get('payment_verified'))


if __name__ == '__main__':
    unittest.main()
