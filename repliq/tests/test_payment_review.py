"""Owner payment decisions and customer wording; all services are local fakes."""

import json
import unittest
from datetime import datetime

from catalog import Catalog
from customer_updates import CustomerUpdates
from order_summary import confirmation_key, snapshot
import test_consultation_flow as consultation
import test_carrier_payment as carrier
import test_fulfillment as legacy


class ConfirmationTests(unittest.TestCase):
    def test_confirmation_is_short_without_marking_unchecked_payment_paid(self):
        for method in ('2', '3'):
            with self.subTest(method=method):
                session=consultation.ConsultationTests();session.setUp()
                session.bot.now=lambda:datetime(2026,11,9,15,0)
                session.order();session.turn(method);session.turn(legacy.ADDRESS)
                reply=session.turn('так, усе правильно')
                self.assertEqual(reply.text,'Дякую! Відправка сьогодні протягом дня.')
                self.assertEqual(session.context['checkout_stage'],'submitted')
                self.assertEqual(session.context['awaiting'],'payment_receipt')
                self.assertFalse(session.context.get('payment_verified'))
                self.assertEqual(session.context['customer_confirmation'],confirmation_key(snapshot(session.catalog,session.context)))
                followup=session.turn('Коли відправите?')
                self.assertNotIn('оплат',followup.text.lower())
                self.assertNotIn('квитанц',followup.text.lower())

    def test_weekend_confirmation_keeps_weekend_dispatch_plan(self):
        session=consultation.ConsultationTests();session.setUp()
        session.bot.now=lambda:datetime(2026,11,14,12,0)
        session.order();session.turn('2');session.turn(legacy.ADDRESS)
        reply=session.turn('так, усе правильно')
        self.assertIn('з понеділка',reply.text)
        self.assertNotIn('оплат',reply.text.lower())


class OwnerReviewTests(unittest.TestCase):
    def setUp(self):
        fixture=carrier.WorkerDelivery();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture,self.f,self.queue=fixture,fixture.f,fixture.service.queue
        self.f.catalog.shop['cod_payment']='deposit'
        self.f.shop.write_text(json.dumps(self.f.catalog.data))
        self.f.catalog=Catalog(self.f.catalog.data)
        self.f.worker.catalog=self.f.worker.base_catalog=self.f.worker.dialogue.catalog=fixture.service.catalog=self.f.catalog
        (self.f.store.path.parent/'payments.json').write_text(json.dumps({'enabled':True,'kind':'card','account':consultation.TEST_CARD}))
        self.f.process('2');self.f.process(legacy.ADDRESS);self.f.process('так, усе правильно')
        self.row=self.queue.updates('a')[0]
        self.f.worker.parcel_updates()

    def drain(self):
        for _ in range(5):self.f.worker.send_customer_update()

    def notices(self):
        return [(thread,body) for thread,body in self.f.client.sent if 'оплату не вдалося підтвердити' in body]

    def reject(self):
        self.assertTrue(self.queue.payment(self.row['id'],self.row['invoice_key'],7,False))
        self.f.worker.parcel_updates()

    def test_waiting_without_owner_decision_does_not_request_receipt(self):
        self.drain()
        self.assertEqual(self.notices(),[])
        self.assertEqual(self.queue.get(self.row['id'])['payment_review_revision'],0)

    def test_explicit_rejection_reaches_only_correct_buyer_once_across_restarts(self):
        self.reject();self.reject();self.drain()
        self.assertEqual(len(self.notices()),1)
        thread,body=self.notices()[0]
        self.assertEqual(thread,'t1')
        self.assertIn('квитанцію ще раз',body)
        self.assertNotIn('оплатіть',body)
        self.assertFalse(self.f.context()['payment_verified'])
        self.assertEqual(self.queue.get(self.row['id'])['payment_review_revision'],1)
        self.f.worker.customer_updates=CustomerUpdates(self.f.store)
        self.f.worker.fulfillment_cursor=0
        self.f.worker.parcel_updates();self.drain()
        self.assertEqual(len(self.notices()),1)

    def test_two_customers_do_not_receive_each_others_review(self):
        self.f.process('2','t2');self.f.process(legacy.ADDRESS,'t2');self.f.process('так, усе правильно','t2')
        self.f.worker.parcel_updates()
        self.reject();self.drain()
        self.assertEqual([thread for thread,_ in self.notices()],['t1'])
        self.assertFalse(self.f.context('t2').get('payment_verified'))

    def test_manual_approval_before_send_cancels_the_receipt_request(self):
        self.reject()
        self.queue.payment(self.row['id'],self.row['invoice_key'],7,True)
        self.f.worker.parcel_updates();self.drain()
        self.assertEqual(self.notices(),[])
        self.assertTrue(self.f.context()['payment_verified'])

    def test_new_receipt_before_send_cancels_the_outdated_request(self):
        self.reject()
        self.queue.attach_receipts(self.row['id'],['https://example.test/new-receipt.jpg'])
        self.f.worker.parcel_updates();self.drain()
        self.assertEqual(self.notices(),[])
        self.assertTrue(self.queue.payment(self.row['id'],self.row['invoice_key'],7,False))
        self.f.worker.parcel_updates();self.drain()
        self.assertEqual(len(self.notices()),1)
        self.assertEqual(self.queue.get(self.row['id'])['payment_review_revision'],2)

    def test_bank_or_network_problem_does_not_accuse_customer_of_failed_payment(self):
        with self.queue.db() as db:
            db.execute("UPDATE parcels SET payment_source='monobank_review',paid=0,updated=updated+1 WHERE id=?",(self.row['id'],))
        self.f.worker.parcel_updates();self.drain()
        self.assertEqual(self.notices(),[])

    def test_stale_invoice_and_already_shipped_order_cannot_trigger_review(self):
        self.assertFalse(self.queue.payment(self.row['id'],'different-invoice',7,False))
        with self.queue.db() as db:
            db.execute("UPDATE parcels SET ledgerly_status='shipped' WHERE id=?",(self.row['id'],))
        self.assertFalse(self.queue.payment(self.row['id'],self.row['invoice_key'],7,False))
        self.f.worker.parcel_updates();self.drain()
        self.assertEqual(self.notices(),[])


if __name__=='__main__':unittest.main()
