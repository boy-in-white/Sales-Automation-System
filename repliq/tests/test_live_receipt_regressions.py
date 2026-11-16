"""Production conversation failures and receipt/bank gates; all payments are synthetic."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import test_consultation_flow as consultation
import test_monobank as bank_tests
from catalog import Catalog
from dialogue import Dialogue
from customer_language import parameters, requested_quantity, intents
from customer_feedback import APOLOGY
from product_registry import runtime_catalog, Registry
from chat_store import Store
from handoff_store import Handoffs
from operator_menu import OperatorMenu

ROOT=Path(__file__).resolve().parents[1]

class BuyerRegressionTests(unittest.TestCase):
    def runtime(self):
        base=Catalog.load(ROOT/'shop.json')
        record={'id':'new-sql-tee','canonical_name':'Футболки Gallery Dept','brand':'','category':'','aliases':['Футболки Gallery Dept']}
        binding={'advert':'102','product_id':record['id'],'active':1,'title':record['canonical_name'],'price':650}
        return runtime_catalog(base,[record],[binding])

    def test_new_plural_sql_product_inherits_only_its_known_model_facts(self):
        catalog=self.runtime();product=catalog.products[catalog.adverts['102']]
        self.assertEqual(product['sizing_basis'],'height')
        self.assertEqual(product['garment_measurements']['M']['width_cm'],45)
        self.assertIn('XL',product['excluded_sizes'])
        self.assertEqual(product['ledgerly_product_id'],'new-sql-tee')

    def test_new_unknown_product_does_not_borrow_gallery_facts(self):
        base=Catalog.load(ROOT/'shop.json')
        for name in ('Футболки Broken Planet','Футболки Gallery Dept інша модель'):
            catalog=runtime_catalog(base,[{'id':'other','canonical_name':name,'aliases':[]}],[])
            product=next(iter(catalog.products.values()))
            self.assertFalse(product.get('garment_measurements'))
            self.assertNotEqual(product.get('sizing_basis'),'height')

    def test_same_plural_titles_remain_ambiguous(self):
        base=Catalog.load(ROOT/'shop.json')
        catalog=runtime_catalog(base,[{'id':str(i),'canonical_name':'Футболки Gallery Dept','aliases':[]} for i in (1,2)],[])
        self.assertTrue(all(not p.get('garment_measurements') for p in catalog.products.values()))

    def test_body_pair_with_polite_prefix_and_conjunction_is_not_quantity(self):
        for text in ('давай 180 і 80','давайте 1.80 и 80','ну, 180 та 80','180 80','так 180/80'):
            with self.subTest(text=text):
                values,errors=parameters(text,{'awaiting':'parameters'})
                self.assertEqual(values,{'height':180,'weight':80})
                self.assertFalse(errors)
                self.assertIsNone(requested_quantity(text))

    def test_real_quantity_is_still_validated(self):
        self.assertEqual(requested_quantity('давай 180 футболок'),180)
        self.assertEqual(requested_quantity('хочу 2 чорні футболки'),2)

    def test_screenshot_conversation_recommends_without_escalating(self):
        bot=Dialogue(self.runtime(),consultation.Stock())
        context={}
        for text in ('так, хочу футболку','який порадиш','давай 180 і 80'):
            plan=bot.plan([text],context,'102')
            context=dict(plan.context,**plan.on_send)
            self.assertFalse(plan.on_send.get('handoff_now'),plan.text)
            self.assertNotIn('кількість цілим',plan.text)
        self.assertIn('Вам підійде L',plan.text)
        self.assertEqual(context['size'],'L')
        self.assertEqual(context.get('quantity',1),1)

    def test_new_recommendation_reconsiders_previous_size(self):
        bot=Dialogue(self.runtime(),consultation.Stock())
        previous={'product':self.runtime().adverts['102'],'size':'M','color':'BLACK','checkout_offered':True,'greeted':True}
        plan=bot.plan(['привіт, порадь який розмір взяти. В мене зріст 170 і вага 60'],previous,'102')
        self.assertIn('Вам підійде L',plan.text)
        self.assertNotIn('Ви обрали M',plan.text)
        self.assertFalse(plan.on_send.get('handoff_now'))

    def test_standalone_cancel_is_not_a_checkout_question(self):
        f=consultation.ConsultationTests();f.setUp();f.order()
        for text in ('скасуй','скасуйте','отмени','будь ласка, скасуй'):
            with self.subTest(text=text):
                self.assertIn('cancel',intents(text))
                p=f.bot.plan([text],f.context,'101')
                self.assertIn('скасовано',p.text)
                self.assertNotIn('Оформлюємо',p.text)
                self.assertFalse(p.context.get('checkout_offered'))

    def test_simple_praise_and_insult_do_not_repeat_old_owner_promise(self):
        f=consultation.ConsultationTests();f.setUp()
        for text in ('красавчик','молодець','красава'):
            p=f.bot.plan([text],{'owner_requested':True,'greeted':True},'101')
            self.assertIn('Звертайтесь',p.text)
            self.assertNotIn('власник',p.text.lower())
            self.assertFalse(p.on_send.get('handoff_now'))
        p=f.bot.plan(['Ти лох'],{'owner_requested':True,'greeted':True},'101')
        self.assertEqual(p.text,APOLOGY)
        self.assertFalse(p.on_send.get('handoff_now'))

    def test_mono_button_queues_a_visible_fresh_response(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'conversations.sqlite3')
            try:
                registry=Registry(store,'account');handoffs=Handoffs(store)
                OperatorMenu(handoffs,registry,'account',-1001).show(7,'press-1','mono')
                with store.db() as db:
                    rows=db.execute("SELECT event_key,text FROM admin_outbox WHERE status='pending'").fetchall()
                self.assertEqual(len(rows),1)
                self.assertTrue(rows[0]['event_key'].startswith('mono-status:'))
                self.assertIn('mono',rows[0]['text'])
            finally: store.close_current()

class ReceiptGateTests(unittest.TestCase):
    def setUp(self):
        self.f=bank_tests.BankTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def order(self):
        row=self.f.order()
        data=copy.deepcopy(row['data']);data['receipt_required']=True
        return self.f.queue.put(data)

    def archive(self,row,url='https://images.example.test/receipt.jpg',state='sent'):
        self.f.queue.attach_receipts(row['id'],[url])
        claim=self.f.queue.claim_receipt();self.assertIsNotNone(claim)
        self.assertEqual(claim['id'],row['id'])
        self.f.queue.receipt_result(row['id'],row['invoice_key'],claim['receipt_hash'],state,-100123,41)

    def test_bank_payment_without_receipt_is_evidence_but_not_approval(self):
        row=self.order();self.f.ingest(self.f.tx())
        saved=self.f.queue.get(row['id'])
        self.assertFalse(saved['paid']);self.assertEqual(saved['payment_source'],'monobank_pending_receipt')
        self.assertEqual(json.loads(saved['bank_payment'])['actual_cents'],10000)
        revision=saved['payment_revision'];self.f.checker.reconcile(self.f.settings)
        self.assertEqual(self.f.queue.get(row['id'])['payment_revision'],revision)

    def test_raw_receipt_link_is_not_enough(self):
        row=self.order();self.f.queue.attach_receipts(row['id'],['https://images.example.test/receipt.jpg'])
        self.f.ingest(self.f.tx());self.assertFalse(self.f.paid(row))

    def test_archived_receipt_without_bank_credit_is_not_payment(self):
        row=self.order();self.archive(row)
        self.f.checker.reconcile(self.f.settings);self.assertFalse(self.f.paid(row))

    def test_both_facts_approve_once_and_preserve_actual_amount(self):
        row=self.order();self.f.ingest(self.f.tx(amount=9400));self.archive(row)
        self.f.checker.reconcile(self.f.settings)
        saved=self.f.queue.get(row['id']);self.assertTrue(saved['paid'])
        self.assertEqual(saved['payment_source'],'monobank')
        self.assertEqual(json.loads(saved['bank_payment'])['actual_cents'],9400)
        revision=saved['payment_revision'];self.f.checker.reconcile(self.f.settings)
        self.assertEqual(self.f.queue.get(row['id'])['payment_revision'],revision)
        self.assertEqual(self.f.queue.receipts(row['id'])[0]['permalink'],'https://t.me/c/123/41')
        self.assertIsNone(self.f.queue.claim_receipt())

    def test_receipt_before_bank_credit_also_works(self):
        row=self.order();self.archive(row);self.f.ingest(self.f.tx())
        self.assertTrue(self.f.paid(row))

    def test_uncertain_or_rejected_archive_does_not_auto_approve(self):
        for state in ('uncertain','failed'):
            f=self.f;row=self.order();self.f.ingest(self.f.tx('credit-'+state));self.archive(row,state=state)
            self.f.checker.reconcile(self.f.settings);self.assertFalse(self.f.paid(row))

    def test_two_payments_need_each_customers_own_receipt(self):
        a,b=self.order(),self.order();self.f.ingest(self.f.tx('a'),self.f.tx('b'))
        self.assertFalse(self.f.paid(a));self.assertFalse(self.f.paid(b))
        self.archive(a);self.f.checker.reconcile(self.f.settings)
        self.assertTrue(self.f.paid(a));self.assertFalse(self.f.paid(b))
        self.archive(b);self.f.checker.reconcile(self.f.settings);self.assertTrue(self.f.paid(b))

    def test_owner_rejection_is_not_overwritten(self):
        row=self.order();self.f.ingest(self.f.tx());self.archive(row)
        self.f.queue.payment(row['id'],row['invoice_key'],7,False)
        self.f.checker.reconcile(self.f.settings);self.assertFalse(self.f.paid(row))

    def test_bank_amendment_invalidates_pending_evidence(self):
        row=self.order();self.f.ingest(self.f.tx());self.f.ingest(self.f.tx(amount=0))
        self.archive(row);self.f.checker.reconcile(self.f.settings)
        self.assertFalse(self.f.paid(row));self.assertEqual(self.f.queue.get(row['id'])['payment_source'],'monobank_review')

    def test_upload_recovery_never_blindly_sends_a_second_copy(self):
        row=self.order();self.f.queue.attach_receipts(row['id'],['https://images.example.test/receipt.jpg'])
        self.assertIsNotNone(self.f.queue.claim_receipt());self.f.queue.recover_receipts()
        self.assertIsNone(self.f.queue.claim_receipt())
        self.assertEqual(self.f.queue.receipts(row['id'])[0]['state'],'uncertain')
