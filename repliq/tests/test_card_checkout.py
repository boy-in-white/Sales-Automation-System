"""Card checkout rollback: fake customers/carrier, no external traffic."""

import copy
import json
from pathlib import Path
import unittest

import test_consultation_flow as consultation
import test_carrier_payment as carrier
import test_fulfillment as legacy
from fulfillment import parcel_payload
from order_summary import invoice_key, snapshot


class CardCheckout(unittest.TestCase):
    def setUp(self):
        self.s = consultation.ConsultationTests()
        self.s.setUp()

    def payload(self):
        self.s.context['fulfillment_opt_in'] = True
        self.s.turn(legacy.ADDRESS)
        self.s.turn('так, все правильно')
        return parcel_payload(self.s.catalog, self.s.context, 'account', 'thread', 'buyer', '101')

    def test_current_shop_offers_card_deposit_without_business_or_ttn_payment_wording(self):
        self.assertEqual(self.s.catalog.shop['cod_payment'], 'deposit')
        for key in ('delivery', 'payment', 'order_instructions'):
            with self.subTest(key=key):
                body = self.s.catalog.shop[key].lower()
                self.assertIn('100 грн', body)
                self.assertIn('картк', body)
                self.assertNotIn('фоп', body)
                self.assertNotIn('за ттн', body)

    def test_cod_invoice_and_waybill_subtract_the_same_deposit(self):
        self.s.order()
        reply = self.s.turn('давайте післяплатою')
        self.assertIn('Картка:', reply.text)
        self.assertIn(consultation.TEST_CARD, reply.text.replace(' ', ''))
        self.assertIn('Сума: 100 грн', reply.text)
        self.assertIn('700 грн', reply.text)
        self.assertNotIn('ФОП', reply.text)
        self.assertNotIn('оплату доставки', reply.text)
        payload = self.payload()
        self.assertRegex(payload['invoice_id'], r'^RQ-[A-F0-9]{12}$')
        self.assertEqual((payload['version'], payload['advance_cents'], payload['cod_cents']), (1, 10000, 70000))
        self.assertNotIn('payment_policy', payload)
        self.assertFalse(self.s.context.get('payment_verified'))

    def test_full_card_payment_leaves_no_goods_cod(self):
        self.s.order()
        reply = self.s.turn('3')
        self.assertIn('Картка:', reply.text)
        self.assertIn('Сума: 800 грн', reply.text)
        self.assertIn('лише доставку', reply.text)
        self.assertNotIn('ФОП', reply.text)
        payload = self.payload()
        self.assertEqual((payload['advance_cents'], payload['cod_cents']), (80000, 0))

    def test_missing_card_does_not_issue_an_invoice(self):
        self.s.bot.merchant = {}
        self.s.order()
        reply = self.s.turn('2')
        self.assertFalse(self.s.context.get('invoice_issued'))
        self.assertFalse(reply.on_send.get('handoff_now'))


class PreviousCarrierOrders(unittest.TestCase):
    def setUp(self):
        self.old = carrier.DeliveryCheckout()
        self.old.setUp()
        self.addCleanup(self.old.doCleanups)

    def test_previous_invoice_is_preserved_without_another_charge_or_automatic_handoff(self):
        self.old.order()
        saved = copy.deepcopy(self.old.s.context)
        before = invoice_key(snapshot(self.old.s.catalog, saved))
        self.old.s.catalog.shop['cod_payment'] = 'deposit'
        reply = self.old.s.turn('Як оплатити доставку?')
        self.assertIn('нічого додатково не оплачуйте', reply.text)
        self.assertFalse(reply.on_send.get('handoff_now'))
        self.assertNotIn('Картка:', reply.text)
        self.assertNotIn('Оберіть оплату доставки', reply.text)
        self.assertEqual(self.old.s.context['invoice_id'], saved['invoice_id'])
        self.assertEqual(self.old.s.context['checkout_amount'], saved['checkout_amount'])
        self.assertEqual(invoice_key(snapshot(self.old.s.catalog, self.old.s.context)), before)
        self.assertIsNone(parcel_payload(self.old.s.catalog, saved, 'account', 'thread', 'buyer', '101'))

    def test_pending_carrier_waybill_is_not_created_after_rollback(self):
        payload = self.old.order()
        row = self.old.service.queue.put(payload)
        self.old.s.catalog.shop['cod_payment'] = 'deposit'
        self.assertFalse(self.old.service.process_one())
        self.assertEqual(self.old.api.calls, [])
        current = self.old.service.queue.get(row['id'])
        self.assertEqual(current['nova_status'], 'pending')
        self.assertEqual(current['data']['cod_cents'], 80000)

    def test_prepared_carrier_waybill_notice_has_no_unavailable_payment_instructions(self):
        fixture = carrier.WorkerDelivery()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        f = fixture.f
        f.process('2')
        f.process(legacy.ADDRESS)
        f.process('так, все правильно')
        self.assertTrue(fixture.service.process_one())
        f.worker.parcel_updates()
        order_id = f.context()['invoice_id']
        f.catalog.shop['cod_payment'] = 'deposit'
        f.shop.write_text(json.dumps(f.catalog.data))
        f.worker.send_customer_update()
        f.worker.send_customer_update()
        recipient, body = f.client.sent[-1]
        self.assertEqual(recipient, 't1')
        self.assertIn('Ось ваша ТТН: ' + legacy.TTN, body)
        self.assertIn(order_id, body)
        self.assertNotIn('оплат', body.lower())
        self.assertIsNone(f.context('t2').get('invoice_id'))


if __name__ == '__main__':
    unittest.main()
