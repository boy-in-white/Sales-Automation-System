"""Offline bank matching and checkout tests. All bank data and tokens are fake."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from app import write_private
from checkout import payment_settings, payment_signature
from fulfillment_queue import ParcelQueue
from monobank_payments import (MIN_DEPOSIT, MAX_DEPOSIT, MonoError, PaymentChecker,
                              bank_marker, destination, fingerprint, marker, scope)
from monobank_setup import personal_accounts, save_selection
from order_summary import invoice_key, snapshot
import test_consultation_flow as consultation
import test_fulfillment as legacy


class BankTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = int(time.time())
        self.merchant = {'enabled':True, 'kind':'card', 'account':consultation.TEST_CARD, 'recipient':''}
        self.settings = {'enabled':True, 'token':'test-token', 'account_id':'dedicated', 'client_id':'owner',
                         'activation_id':'activation', 'enabled_at':self.now-1000, 'dedicated_account':True,
                         'destination_signature':destination(self.merchant), 'own_iban_hashes':[fingerprint('OWN-IBAN')]}
        write_private(self.root/'payments.json', self.merchant)
        write_private(self.root/'monobank.json', self.settings)
        self.queue = ParcelQueue(self.root/'bridge.sqlite3')
        self.stop = threading.Event()
        self.log = []
        self.checker = PaymentChecker(self.root,self.queue,self.stop,report=self.log.append,clock=lambda:self.now)
        self.serial = 0

    def order(self, method='cod', at=None, **tag_changes):
        self.serial += 1
        invoice = 'RQ-%012X' % self.serial
        key = fingerprint(invoice)
        data = {'version':1,'account':'olx','thread':'t%s'%self.serial,'buyer':'b%s'%self.serial,
                'advert':'101','invoice_id':invoice,'invoice_key':key,'method':method,
                'total_cents':80000,'advance_cents':10000 if method=='cod' else 80000,
                'cod_cents':70000 if method=='cod' else 0,
                'bank_payment':dict(scope=scope(self.settings),activation_id='activation',
                    destination_signature=destination(self.merchant),invoice_key=key,
                    requested_at=self.now-100 if at is None else at,**tag_changes)}
        return self.queue.put(data)

    def tx(self, identifier='p1', amount=10000, at=None, **changes):
        return dict({'id':identifier, 'amount':amount, 'time':self.now-50 if at is None else at,
                     'currencyCode':980, 'hold':False, 'description':'Переказ на картку'}, **changes)

    def ingest(self, *transactions):
        self.checker.ingest(self.settings, list(transactions))
        return self.checker.reconcile(self.settings)

    def paid(self,row):
        return self.queue.get(row['id'])['paid']

    def archive_receipt(self, row):
        self.queue.attach_receipts(row['id'], ['https://images.example.test/receipt.jpg'])
        claim = self.queue.claim_receipt()
        self.assertEqual(claim['id'], row['id'])
        self.queue.receipt_result(row['id'],row['invoice_key'],claim['receipt_hash'],'sent',-100123,41)

    def test_two_equal_payments_cover_two_customers_but_one_is_not_enough(self):
        one,two = self.order(),self.order()
        self.assertEqual(self.ingest(self.tx()),0)
        self.assertFalse(self.paid(one)); self.assertFalse(self.paid(two))
        self.assertEqual(self.ingest(self.tx('p2')),2)
        self.assertTrue(self.paid(one)); self.assertTrue(self.paid(two))
        self.assertEqual(self.checker.reconcile(self.settings),0)

    def test_deposit_boundaries_and_actual_amount_are_preserved(self):
        row = self.order()
        self.assertEqual(self.ingest(self.tx('low',MIN_DEPOSIT-1),self.tx('high',MAX_DEPOSIT+1)),0)
        self.assertEqual(self.ingest(self.tx('accepted',MIN_DEPOSIT)),1)
        saved = self.queue.get(row['id'])
        proof = json.loads(saved['bank_payment'])
        self.assertEqual((proof['actual_cents'],proof['credited_cents']),(9400,10000))
        self.assertEqual(saved['data']['cod_cents'],70000)
        self.assertEqual(saved['payment_source'],'monobank')

    def test_upper_boundary_is_accepted(self):
        row=self.order()
        self.assertEqual(self.ingest(self.tx(amount=MAX_DEPOSIT)),1)
        self.assertTrue(self.paid(row))

    def test_repeated_statement_and_restart_do_not_reuse_credit(self):
        row=self.order(); self.ingest(self.tx()); self.assertTrue(self.paid(row))
        two=self.order()
        self.checker=PaymentChecker(self.root,self.queue,self.stop,clock=lambda:self.now)
        self.assertEqual(self.ingest(self.tx()),0)
        self.assertFalse(self.paid(two))

    def test_payment_before_invoice_or_activation_is_not_used(self):
        row=self.order()
        self.assertEqual(self.ingest(self.tx('old1',at=self.now-1001),self.tx('old2',at=self.now-101)),0)
        self.assertFalse(self.paid(row))

    def test_expenses_foreign_currency_holds_refunds_and_own_transfers_are_not_payments(self):
        row=self.order()
        transactions=[self.tx('expense',-10000),self.tx('currency',currencyCode=840),self.tx('hold',hold=True),
                      self.tx('own',counterIban='OWN-IBAN'),self.tx('refund',description='Повернення коштів'),
                      self.tx('cashback',description='Кешбек'),self.tx('missing-hold')]
        transactions[-1].pop('hold')
        self.assertEqual(self.ingest(*transactions),0)
        self.assertFalse(self.paid(row))
        self.assertEqual(self.ingest(self.tx('hold',hold=False)),1)

    def test_exact_order_reference_does_not_pay_a_different_customer(self):
        one,two=self.order(),self.order()
        self.assertEqual(self.ingest(self.tx(comment=two['invoice_id'])),1)
        self.assertFalse(self.paid(one)); self.assertTrue(self.paid(two))

    def test_reference_to_unknown_order_is_not_reassigned_to_someone_else(self):
        row=self.order()
        self.assertEqual(self.ingest(self.tx(comment='RQ-FFFFFFFFFFFF')),0)
        self.assertFalse(self.paid(row))

    def test_full_payment_stays_manual_and_other_amounts_are_not_stored(self):
        row=self.order('full')
        self.assertEqual(self.ingest(self.tx('short',79999),self.tx('deposit',10000)),0)
        self.assertEqual(self.ingest(self.tx('full',80000)),0)
        self.assertFalse(self.paid(row))
        self.assertIsNone(self.checker.pending_since(self.settings))
        with self.queue.db() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT transaction_id FROM mono_transactions')],['deposit'])

    def test_other_full_orders_do_not_block_deposit_matching(self):
        for _ in range(101):self.order('full')
        deposit=self.order()
        self.assertEqual(self.ingest(self.tx()),1)
        self.assertTrue(self.paid(deposit))

    def test_full_price_of_100_is_not_treated_as_cod_deposit(self):
        row=self.order('full')
        payload=dict(row['data'],total_cents=10000,advance_cents=10000)
        row=self.queue.put(payload)
        self.assertIsNone(self.checker.pending_since(self.settings))
        self.assertEqual(self.ingest(self.tx()),0)
        self.assertFalse(self.paid(row))

    def test_cancelled_and_manual_orders_cannot_lend_their_anonymous_payment(self):
        one,two=self.order(),self.order()
        self.queue.payment(one['id'],one['invoice_key'],7,True)
        self.assertEqual(self.ingest(self.tx()),0)
        self.assertFalse(self.paid(two))
        self.assertEqual(self.ingest(self.tx('explicit',comment=two['invoice_id'])),1)
        self.queue.payment(two['id'],two['invoice_key'],7,False)
        self.assertEqual(self.ingest(self.tx('again',comment=two['invoice_id'])),0)
        self.assertFalse(self.paid(two))

    def test_cancellation_never_moves_a_consumed_payment_to_another_order(self):
        one=self.order(); self.ingest(self.tx())
        self.queue.cancel(one['account'],one['thread'],one['invoice_id'])
        two=self.order()
        self.assertEqual(self.checker.reconcile(self.settings),0)
        self.assertFalse(self.paid(two))

    def test_amended_bank_transaction_requires_review_but_does_not_overwrite_owner(self):
        row=self.order(); self.ingest(self.tx())
        self.ingest(self.tx(amount=0))
        self.assertFalse(self.paid(row))
        self.assertEqual(self.queue.get(row['id'])['payment_source'],'monobank_review')
        self.queue.payment(row['id'],row['invoice_key'],7,True)
        self.ingest(self.tx(amount=0))
        self.assertTrue(self.paid(row))
        self.assertEqual(self.queue.get(row['id'])['payment_source'],'owner')

    def test_unmarked_legacy_invoice_and_wrong_invoice_binding_are_not_auto_paid(self):
        row=self.order()
        payload=copy.deepcopy(row['data']); payload.pop('bank_payment')
        self.queue.put(payload)
        wrong=self.order()
        payload=copy.deepcopy(wrong['data']); payload['bank_payment']['invoice_key']='different'
        self.queue.put(payload)
        self.assertEqual(self.ingest(self.tx(),self.tx('p2')),0)

    def test_poll_uses_only_selected_account_and_survives_restart_rate_limit(self):
        self.order()
        calls=[]
        settings=self.settings
        class Api:
            def __init__(self,token):
                self.assert_token=token
            def info(self):
                calls.append('info')
                return {'clientId':'owner','accounts':[
                    {'id':'household','currencyCode':980,'type':'black','maskedPan':['411111******2222']},
                    {'id':'dedicated','currencyCode':980,'type':'white','maskedPan':['411111******1111']}]}
            def statement(self,account,start,end):
                calls.append(account)
                return []
        checker=PaymentChecker(self.root,self.queue,self.stop,client_factory=Api,clock=lambda:self.now)
        checker.tick(); checker.tick()
        other=PaymentChecker(self.root,self.queue,self.stop,client_factory=Api,clock=lambda:self.now)
        other.tick()
        self.assertEqual(calls,['info','dedicated'])
        self.now+=65;other.tick()
        self.assertEqual(calls,['info','dedicated','dedicated'])

    def test_wrong_account_and_network_errors_never_auto_confirm(self):
        row=self.order()
        class Api:
            def __init__(self,token):pass
            def info(self): raise MonoError(429)
        checker=PaymentChecker(self.root,self.queue,self.stop,self.log.append,client_factory=Api,clock=lambda:self.now)
        checker.tick()
        self.assertFalse(self.stop.is_set()); self.assertFalse(self.paid(row))
        self.assertIn('429',self.log[-1]); self.assertNotIn('test-token',self.log[-1])

    def test_switching_account_id_cannot_reuse_validation_of_another_card(self):
        self.order()
        calls=[]
        class Api:
            def __init__(self,token):pass
            def info(self):
                return {'clientId':'owner','accounts':[
                    {'id':'dedicated','currencyCode':980,'type':'black','maskedPan':['411111******1111']},
                    {'id':'household','currencyCode':980,'type':'black','maskedPan':['411111******2222']}]}
            def statement(self,account,start,end):
                calls.append(account);return []
        checker=PaymentChecker(self.root,self.queue,self.stop,report=self.log.append,client_factory=Api,clock=lambda:self.now)
        checker.tick()
        changed=dict(self.settings,account_id='household')
        write_private(self.root/'monobank.json',changed)
        self.settings=changed
        self.order()
        self.now+=65;checker.tick()
        self.assertEqual(calls,['dedicated'])
        self.assertTrue(self.log)

    def test_idle_and_full_payment_have_no_bank_requests(self):
        def forbidden(token):
            self.fail('Bank client must not be created without an awaited deposit')
        checker=PaymentChecker(self.root,self.queue,self.stop,client_factory=forbidden,clock=lambda:self.now)
        checker.tick()
        self.order('full')
        self.now+=70;checker.tick()
        self.assertIsNone(checker.pending_since(self.settings))

    def test_bank_requests_stop_after_payment_and_resume_only_for_new_deposit(self):
        row=self.order()
        fixture=self;calls=[]
        class Api:
            def __init__(self,token):pass
            def info(self):
                calls.append('info')
                return {'clientId':'owner','accounts':[{'id':'dedicated','currencyCode':980,'type':'white','maskedPan':['411111******1111']}]}
            def statement(self,account,start,end):
                calls.append('statement');return [fixture.tx()]
        checker=PaymentChecker(self.root,self.queue,self.stop,client_factory=Api,clock=lambda:self.now)
        checker.tick();self.assertTrue(self.paid(row))
        self.now+=130;checker.tick()
        self.assertEqual(calls,['info','statement'])
        self.order(at=self.now-10)
        self.now+=5;checker.tick()
        self.assertEqual(calls,['info','statement','statement'])

    def test_cancelled_expired_or_manual_requests_do_not_poll_bank(self):
        row=self.order()
        self.queue.cancel(row['account'],row['thread'],row['invoice_id'])
        expired=self.order(at=self.now-86401)
        manual=self.order()
        self.queue.payment(manual['id'],manual['invoice_key'],7,False)
        def forbidden(token): self.fail('No live deposit request, no bank requests')
        checker=PaymentChecker(self.root,self.queue,self.stop,client_factory=forbidden,clock=lambda:self.now)
        checker.tick()
        self.assertIsNone(checker.pending_since(self.settings))
        self.assertFalse(self.paid(expired))

    def test_three_orders_survive_every_payment_arrival_order_and_duplicate(self):
        from itertools import permutations
        for sequence in permutations(range(3)):
            with self.subTest(sequence=sequence):
                with self.queue.db() as db:
                    for table in ('mono_matches','mono_transactions','mono_orders','parcels'):
                        db.execute('DELETE FROM '+table)
                orders=[self.order() for _ in range(3)]
                transactions=[self.tx('p%s'%i,amount) for i,amount in enumerate((9400,10000,10500))]
                for position,index in enumerate(sequence):
                    self.ingest(transactions[index],transactions[index])
                    self.assertEqual(sum(self.paid(row) for row in orders),3 if position==2 else 0)
                restarted=PaymentChecker(self.root,self.queue,self.stop,clock=lambda:self.now)
                self.assertEqual(restarted.reconcile(self.settings),0)
                with self.queue.db() as db:
                    pairs=db.execute('SELECT parcel_id,transaction_id FROM mono_matches').fetchall()
                self.assertEqual(len(pairs),3)
                self.assertEqual(len({r['parcel_id'] for r in pairs}),3)
                self.assertEqual(len({r['transaction_id'] for r in pairs}),3)

    def test_late_posting_is_found_after_outage_and_restart_without_idle_requests(self):
        row=self.order(at=self.now-900)
        payment=self.tx(at=self.now-850)
        fixture=self;calls=[]
        class Api:
            def __init__(self,token):pass
            def info(self):
                return {'clientId':'owner','accounts':[{'id':'dedicated','currencyCode':980,'type':'white','maskedPan':['411111******1111']}]}
            def statement(self,account,start,end):
                calls.append((start,end))
                if len(calls)==1:raise MonoError('network')
                fixture.assertLessEqual(start,payment['time'])
                return [payment]
        checker=PaymentChecker(self.root,self.queue,self.stop,client_factory=Api,clock=lambda:self.now)
        checker.tick();self.assertFalse(self.paid(row))
        self.now+=10
        restarted=PaymentChecker(self.root,self.queue,self.stop,client_factory=Api,clock=lambda:self.now)
        restarted.tick();self.assertEqual(len(calls),1)
        self.now+=1200;restarted.tick()
        self.assertTrue(self.paid(row));self.assertEqual(len(calls),2)
        self.now+=130;restarted.tick();self.assertEqual(len(calls),2)

    def test_bank_cooldown_is_persisted_across_restart(self):
        self.order();calls=[]
        class Api:
            def __init__(self,token):pass
            def info(self):
                calls.append('info');raise MonoError(429,retry_after=1200)
        checker=PaymentChecker(self.root,self.queue,self.stop,client_factory=Api,clock=lambda:self.now)
        checker.tick();self.assertEqual(calls,['info'])
        self.now+=301
        restarted=PaymentChecker(self.root,self.queue,self.stop,client_factory=Api,clock=lambda:self.now)
        restarted.tick();self.assertEqual(calls,['info'])
        self.now+=900;restarted.tick();self.assertEqual(calls,['info','info'])

    def test_changed_payment_destination_disables_auto_mode(self):
        changed=dict(self.merchant,account='5555555555554444')
        self.assertIsNone(bank_marker(self.root,changed))
        self.settings['account_id']='0'
        write_private(self.root/'monobank.json',self.settings)
        self.assertIsNone(bank_marker(self.root,self.merchant))

    def test_early_payment_is_reserved_before_shipping_details_and_applied_to_same_invoice(self):
        from fulfillment import parcel_payload
        s=consultation.ConsultationTests();s.setUp()
        s.order();s.turn('2')
        key=invoice_key(snapshot(s.catalog,s.context))
        s.context['bank_payment']=marker(bank_marker(self.root,self.merchant),key,self.now-100)
        self.checker.watch_invoice('olx','early',s.context,s.catalog)
        self.assertEqual(self.ingest(self.tx()),1)
        s.context['fulfillment_opt_in']=True
        s.turn(legacy.ADDRESS);s.turn('так')
        payload=parcel_payload(s.catalog,s.context,'olx','early','buyer','101')
        row=self.queue.put(payload)
        self.assertFalse(row['paid'])
        self.checker.reconcile(self.settings)
        self.assertFalse(self.paid(row))
        self.archive_receipt(row)
        self.checker.reconcile(self.settings)
        self.assertTrue(self.paid(row))
        self.assertEqual(self.queue.get(row['id'])['data']['cod_cents'],70000)

    def test_three_early_payments_survive_restart_and_shipping_details_in_reverse_order(self):
        from fulfillment import parcel_payload
        customers=[]
        for index in range(3):
            s=consultation.ConsultationTests();s.setUp();s.order();s.turn('2')
            key=invoice_key(snapshot(s.catalog,s.context))
            s.context['bank_payment']=marker(bank_marker(self.root,self.merchant),key,self.now-100)
            self.checker.watch_invoice('olx','early-%s'%index,s.context,s.catalog)
            customers.append(s)
        self.assertEqual(self.ingest(self.tx('one'),self.tx('two'),self.tx('three')),3)
        self.assertIsNone(self.checker.pending_since(self.settings))
        restarted=PaymentChecker(self.root,self.queue,self.stop,clock=lambda:self.now)
        created=[]
        for index in (2,1,0):
            s=customers[index]
            s.context['fulfillment_opt_in']=True
            s.turn(legacy.ADDRESS);s.turn('так')
            row=self.queue.put(parcel_payload(s.catalog,s.context,'olx','early-%s'%index,'b%s'%index,'101'))
            restarted.reconcile(self.settings)
            self.assertFalse(self.paid(row))
            self.archive_receipt(row)
            restarted.reconcile(self.settings)
            self.assertTrue(self.paid(row))
            created.append(row['id'])
        with self.queue.db() as db:
            matches=db.execute('SELECT parcel_id,transaction_id FROM mono_matches').fetchall()
        self.assertEqual({r['parcel_id'] for r in matches},set(created))
        self.assertEqual(len({r['transaction_id'] for r in matches}),3)

    def test_unfinished_second_order_is_included_in_competing_payment_group(self):
        s=consultation.ConsultationTests();s.setUp()
        s.order();s.turn('2')
        key=invoice_key(snapshot(s.catalog,s.context))
        s.context['bank_payment']=marker(bank_marker(self.root,self.merchant),key,self.now-100)
        self.checker.watch_invoice('olx','unfinished',s.context,s.catalog)
        ready=self.order()
        self.assertEqual(self.ingest(self.tx()),0)
        self.assertFalse(self.paid(ready))
        self.assertEqual(self.ingest(self.tx('p2')),2)
        self.assertTrue(self.paid(ready))

    def test_parallel_checkers_cannot_allocate_one_credit_twice(self):
        row=self.order()
        self.checker.ingest(self.settings,[self.tx()])
        results=[]
        def run():
            results.append(self.checker.reconcile(self.settings))
        threads=[threading.Thread(target=run) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(sorted(results),[0,1])
        self.assertTrue(self.paid(row))

    def test_olx_worker_requires_receipt_before_bank_approval_and_ttn_notice(self):
        import test_carrier_payment as carrier
        fixture=carrier.WorkerDelivery();fixture.setUp();self.addCleanup(fixture.doCleanups)
        f=fixture.f
        f.catalog.shop['cod_payment']='deposit';f.shop.write_text(json.dumps(f.catalog.data))
        from catalog import Catalog
        f.catalog=Catalog(f.catalog.data)
        f.worker.catalog=f.worker.base_catalog=f.worker.dialogue.catalog=fixture.service.catalog=f.catalog
        root=f.store.path.parent
        write_private(root/'payments.json',self.merchant)
        write_private(root/'monobank.json',self.settings)
        f.worker.monobank=PaymentChecker(root,fixture.service.queue,f.worker.stop,clock=lambda:self.now+10)
        f.process('2')
        context=f.context()
        self.assertIn('bank_payment',context)
        self.assertIn('фото або скріншот квитанції',f.client.sent[-1][1])
        payment=self.tx(at=context['bank_payment']['requested_at']+1)
        f.worker.monobank.ingest(self.settings,[payment])
        self.assertEqual(f.worker.monobank.reconcile(self.settings),1)
        f.process(legacy.ADDRESS);f.process('так, усе правильно')
        f.worker.monobank.reconcile(self.settings)
        row = fixture.service.queue.updates('a')[0]
        self.assertFalse(row['paid'])
        f.client.receive('Ось квитанція')
        f.client.histories['t1'][-1]['attachments']=[{'url':'https://images.example.test/worker.jpg'}]
        f.worker.collect('t1',f.client.thread('t1'))
        for buffer in f.worker.buffers.values(): buffer['due']=0
        f.worker.build();f.worker.send_approved()
        self.assertIn('https://images.example.test/worker.jpg',fixture.service.queue.get(row['id'])['receipt_links'])
        self.assertIn('квитанцію отримав',f.client.sent[-1][1])
        # Fake Telegram has retained this invoice's image; no network is used.
        archive=fixture.service.queue.claim_receipt()
        fixture.service.queue.receipt_result(row['id'],row['invoice_key'],archive['receipt_hash'],'sent',-100123,42)
        f.worker.monobank.reconcile(self.settings)
        self.assertTrue(fixture.service.process_one())
        f.worker.parcel_updates()
        self.assertTrue(f.context()['payment_verified'])
        self.assertEqual(f.context()['verified_invoice_key'],invoice_key(snapshot(f.catalog,f.context())))
        for _ in range(4): f.worker.send_customer_update()
        messages=[body for thread,body in f.client.sent if thread=='t1']
        self.assertTrue(any('Ось ваша ТТН: '+legacy.TTN in body for body in messages))
        self.assertFalse(f.context('t2').get('payment_verified'))


class CheckoutTests(unittest.TestCase):
    def test_full_payment_never_promises_automatic_verification(self):
        s=consultation.ConsultationTests();s.setUp()
        s.bot.merchant['_monobank']={'scope':'bank','activation_id':'activation','destination_signature':payment_signature(s.bot.merchant)}
        s.order();reply=s.turn('3')
        self.assertNotIn('перевіримо автоматично',reply.text)
        self.assertIn('квитанції',reply.text)

    def test_automatic_checkout_requests_receipt_and_collects_shipping(self):
        s=consultation.ConsultationTests();s.setUp()
        s.bot.merchant['_monobank']={'scope':'bank','activation_id':'activation','destination_signature':payment_signature(s.bot.merchant)}
        s.order(); reply=s.turn('2')
        self.assertIn('фото або скріншот квитанції',reply.text)
        key=invoice_key(snapshot(s.catalog,s.context))
        s.context['bank_payment']=marker(s.bot.merchant['_monobank'],key)
        s.turn(legacy.ADDRESS); reply=s.turn('так, усе правильно')
        self.assertTrue(s.context.get('customer_confirmation'))
        self.assertEqual(s.context['checkout_stage'],'submitted')
        self.assertEqual(s.context.get('awaiting'),'payment_receipt')
        self.assertFalse(s.context.get('payment_verified'))
        reply=s.turn('Квитанцію потрібно скидати?')
        self.assertIn('фото або скріншот квитанції',reply.text)

    def test_runtime_marker_does_not_change_existing_invoice_signature(self):
        merchant={'enabled':True,'kind':'card','account':consultation.TEST_CARD}
        self.assertEqual(payment_signature(merchant),payment_signature(dict(merchant,_monobank={'scope':'bank'})))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        # Generate a valid test IBAN without using any real person's bank details.
        tail='322001'+'0'*19
        checksum=98-int(tail+'301000')%97
        self.iban='UA%02d%s'%(checksum,tail)
        self.account={'id':'chosen','currencyCode':980,'type':'black','maskedPan':['411111******1111'],'iban':self.iban}
        self.info={'clientId':'owner','name':'Тестовий отримувач','accounts':[self.account]}
        self.merchant={'enabled':True,'kind':'card','account':consultation.TEST_CARD,'recipient':''}

    def test_setup_pins_account_and_writes_private_settings_without_losing_activation_on_token_rotation(self):
        cfg=save_selection(self.root,'fake-token',self.info,self.account,self.merchant,True)
        self.assertEqual(personal_accounts(self.info),[self.account])
        self.assertEqual(cfg['account_id'],'chosen')
        self.assertEqual((self.root/'monobank.json').stat().st_mode&0o777,0o600)
        merchant=payment_settings(self.root)
        self.assertIn('_monobank',merchant)
        rotated=save_selection(self.root,'new-token',self.info,self.account,self.merchant,True)
        self.assertEqual(rotated['activation_id'],cfg['activation_id'])
        self.assertEqual(rotated['enabled_at'],cfg['enabled_at'])

    def test_setup_rejects_household_mismatch_missing_selection_and_business_or_foreign_accounts(self):
        with self.assertRaises(ValueError):
            save_selection(self.root,'fake-token',self.info,self.account,dict(self.merchant,account='5555555555554444'),True)
        with self.assertRaises(ValueError):
            save_selection(self.root,'fake-token',self.info,self.account,self.merchant,False)
        data=copy.deepcopy(self.info)
        data['accounts']=[dict(self.account,type='fop'),dict(self.account,currencyCode=840)]
        self.assertEqual(personal_accounts(data),[])
        self.assertFalse((self.root/'monobank.json').exists())


if __name__=='__main__':
    unittest.main()
