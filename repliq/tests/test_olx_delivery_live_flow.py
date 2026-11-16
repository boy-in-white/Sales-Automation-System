"""Isolated integration checks: fake OLX/Telegram, real queues and dialogue."""
import copy
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import AppError
import app
from buyer_identity import BuyerNames, topic_title
from catalog import Catalog
from chat_messages import candidate, ordered
from chat_store import Store
from dialogue import Dialogue
from handoff_store import Handoffs, HUMAN_IDLE_SECONDS
from inventory import DemoInventory
from olx_delivery import queue_alert, order_report
from product_registry import Registry
from sales_service import Worker
from shop_facts import shop_day, FULL_PREPAYMENT_REFUND, COD_DEPOSIT_REFUND
from telegram_bridge import Bridge
from telegram_errors import TelegramError
from checkout import payment_settings, valid_iban
from customer_phrasing import cod_deposit_question

ROOT=Path(__file__).resolve().parents[1]
NOTICE='У вас придбали товар з OLX Доставкою! Підтвердьте купівлю протягом 72 годин на вкладці «OLX Доставка».'
CONFIG={'bot_token':'test','owner_ids':[7],'group_id':-100123}

class FakeOlx:
    def __init__(self):self.histories={'t1':[],'t2':[]};self.sent=[];self.profile_reads=[]
    def receive(self, text, thread='t1', kind='received'):
        history=self.histories[thread];i=len(history)+1
        history.append({'id':i,'uuid':thread+'-m'+str(i),'thread_uuid':thread,'type':kind,'text':text,
                        'created_at':datetime.now(timezone.utc).isoformat(),'attachments':[]})
    def thread(self, thread):
        return {'uuid':thread,'advert_id':102,'interlocutor_id':123 if thread=='t1' else 456,'total_count':len(self.histories[thread])}
    def identity(self):return {'id':'a'}
    def messages(self,thread):return copy.deepcopy(self.histories[thread])
    def send(self,thread,text):
        self.sent.append((thread,text));self.receive(text,thread,'sent');return copy.deepcopy(self.histories[thread][-1])
    def user(self,user):self.profile_reads.append(user);return {'id':int(user),'name':'Олена' if str(user)=='123' else 'Андрій'}

class FakeTelegram:
    def __init__(self,error=None):self.calls=[];self.error=error
    def call(self,method,fields,document=None):
        self.calls.append((method,fields))
        if self.error and method=='sendMessage':raise self.error
        if method=='sendMessage':return {'message_id':1000,'chat':{'id':fields['chat_id'],'type':'private'},'date':int(time.time())}
        if method in ('answerCallbackQuery','editMessageReplyMarkup','editForumTopic'):return True
        if method=='editMessageText':return {'message_id':fields['message_id']}
        raise AssertionError('Unexpected Telegram call: '+method)

class DeliveryFlow(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)
        data=json.loads((ROOT/'shop.json').read_text())
        for i,p in enumerate(data['products']):p.update(advert_ids=[101+i],ledgerly_product_id=p['key'],price_uah=800 if i==0 else 650)
        self.catalog=Catalog(data);self.shop=self.path/'shop.json';self.shop.write_text(json.dumps(data))
        self.store=Store(self.path/'private'/'state.sqlite3');self.addCleanup(self.store.close_current)
        self.client=FakeOlx();self.worker=Worker(self.client,self.store,self.catalog,self.shop,DemoInventory(),auto_send=True)
        self.worker.account='a';self.worker.telegram_config=copy.deepcopy(CONFIG);self.worker.registry=Registry(self.store,'a')
        self.worker.started_at=time.time()-60;self.store.begin('a',self.worker.session)
        self.bridge=Bridge(self.worker.handoffs,CONFIG,'a',threading.Event(),api=FakeTelegram())
        self.addCleanup(self.bridge.history.close)
        for thread,buyer in (('t1','123'),('t2','456')):
            self.client.receive('Попереднє повідомлення',thread,'sent');self.worker.counts[thread]=1
            ctx={'product':self.catalog.adverts['102'],'size':'M','color':'BLACK','checkout_offered':True,'greeted':True}
            self.store.save_context('a',thread,buyer,'102',ctx)
            self.worker.handoffs.watch('a',thread,buyer,'102',1,'Клієнт '+buyer)
        with self.store.db() as db:db.execute("INSERT INTO effective_ads VALUES('a','102',?)",(self.catalog.adverts['102'],))
    def context(self,thread='t1'):
        return self.store.context('a',thread,'123' if thread=='t1' else '456','102',30)
    def process(self,text,thread='t1'):
        self.client.receive(text,thread);self.worker.collect(thread,self.client.thread(thread))
        for buf in self.worker.buffers.values():buf['due']=0
        self.worker.build();self.worker.send_approved()
    def alerts(self):
        with self.store.db() as db:return [dict(r) for r in db.execute("SELECT * FROM admin_outbox WHERE event_key LIKE 'olx-delivery:%' ORDER BY id")]
    def test_real_template_to_private_queue_and_customer_ack(self):
        self.process('Олх доставкою, Укрпошта')
        self.assertEqual(self.context()['checkout_method'],'olx');self.assertEqual(self.alerts(),[])
        self.assertFalse(self.context().get('invoice_issued'));self.assertIsNone(self.worker.fulfillment.queue)
        self.process(NOTICE)
        self.assertEqual(len(self.alerts()),1);self.assertEqual(self.alerts()[0]['owner'],7)
        self.assertIn('Дякую за покупку',self.client.sent[-1][1])
        expected='сьогодні протягом дня' if shop_day(self.catalog.shop).weekday()<5 else 'з понеділка'
        self.assertIn(expected,self.client.sent[-1][1])
        self.assertEqual(self.worker.handoffs.get('a','t1')['mode'],'bot')
        self.assertFalse(self.context().get('payment_verified'));self.assertFalse(self.context().get('invoice_issued'))
        before=len(self.client.sent);self.worker.collect('t1',self.client.thread('t1'));self.worker.build();self.worker.send_approved()
        self.assertEqual(len(self.client.sent),before);self.assertEqual(len(self.alerts()),1)
    def test_claim_and_later_notice_do_not_repeat_customer_thanks(self):
        self.process('Олх доставкою');self.process('Оформив')
        self.assertIn('Дякую за покупку',self.client.sent[-1][1]);self.assertEqual(len(self.alerts()),1)
        count=len(self.client.sent);self.process(NOTICE)
        self.assertEqual(len(self.client.sent),count);self.assertEqual(len(self.alerts()),2) # report -> separate API signal
        self.assertEqual(self.worker.handoffs.get('a','t1')['mode'],'bot')
    def test_repeated_signal_and_restart_keep_one_outbox_event(self):
        self.process('Олх доставкою');self.process(NOTICE)
        history=ordered(self.client.messages('t1'),'t1',self.catalog.timezone)
        for _ in range(3):self.worker.delivery_alerts('t1','123','102',history)
        other=Registry(self.store,'a')
        queue_alert(other,CONFIG,'a','t1','123','102',self.context(),history,'Футболка')
        self.assertEqual(len(self.alerts()),1)
    def test_multiple_clients_and_manual_mode_are_isolated(self):
        self.worker.handoffs.mode('a','t2','human')
        self.process(NOTICE,'t1');self.process(NOTICE,'t2')
        self.assertEqual(len(self.alerts()),2)
        self.assertEqual(self.worker.handoffs.get('a','t1')['mode'],'bot')
        self.assertEqual(self.worker.handoffs.get('a','t2')['mode'],'human')
        self.assertTrue(all(thread=='t1' for thread,_ in self.client.sent))
    def test_old_history_is_not_replayed(self):
        self.client.receive(NOTICE);self.worker.counts['t1']=len(self.client.histories['t1'])
        self.worker.collect('t1',self.client.thread('t1'));self.worker.build()
        self.assertEqual(self.alerts(),[]);self.assertEqual(self.client.sent,[])
    def test_ukr_rules_and_weekend_schedule(self):
        for text in ('Можна Укрпоштою?','укр поштою наложкою можна?','Укрпочтой','Ukrposhta'):
            p=self.worker.dialogue.plan([text],{},'102');self.assertIn('лише через OLX',p.text);self.assertFalse(p.on_send.get('handoff_now'));self.assertFalse(p.on_send.get('invoice_issued'))
        for day,expected in ((9,'сьогодні протягом дня'),(14,'з понеділка'),(15,'з понеділка')):
            bot=Dialogue(self.catalog,DemoInventory(),now=lambda day=day:datetime(2026,11,day,10,tzinfo=timezone.utc))
            p=bot.plan([],{},'102',delivery_notice=True)
            self.assertIn(expected,p.text);self.assertIn('Дякую за покупку',p.text);self.assertFalse(p.on_send.get('payment_verified'))
    def test_future_or_negated_customer_text_is_not_an_order(self):
        for text in ('Оформлю','Ще не оформив','Не замовив','Завтра замовлю','Я у іншого замовив'):
            self.assertFalse(order_report(text),text)
    def test_private_api_timeout_is_uncertain_without_auto_retry(self):
        self.process(NOTICE);api=FakeTelegram(TelegramError());self.bridge.api=api
        with self.assertRaises(TelegramError):self.bridge.deliver_private()
        self.bridge.deliver_private()
        self.assertEqual(len(api.calls),1);self.assertEqual(self.alerts()[0]['status'],'uncertain')
        self.assertEqual(self.worker.handoffs.get('a','t1')['mode'],'bot')
    def test_private_delivery_targets_owner_only(self):
        self.process(NOTICE);api=FakeTelegram();self.bridge.api=api;self.bridge.deliver_private()
        self.assertEqual(self.alerts()[0]['status'],'sent');self.assertEqual(api.calls[0][1]['chat_id'],7)
        self.assertNotIn('message_thread_id',api.calls[0][1])
    def test_names_are_cached_and_headers_put_name_then_id(self):
        self.worker.conversation_title('123','102');self.worker.refresh_buyer_names()
        title=self.worker.handoffs.get('a','t1')['title'];self.assertTrue(title.startswith('Олена · ID 123'))
        self.worker.conversation_title('123','102');self.worker.refresh_buyer_names();self.assertEqual(self.client.profile_reads,['123'])
        self.assertNotIn('Олена',self.worker.handoffs.get('a','t2')['title'])
    def test_exactly_two_buttons_for_all_states(self):
        for mode in ('bot','human','paused','closed','blocked','returning'):
            for expanded in (True,False):
                rows=Bridge.controls(mode,expanded=expanded)['inline_keyboard'];self.assertEqual(sum(map(len,rows)),2)
                self.assertEqual([b['text'].replace('✓ ','') for b in rows[0]],['auto','я'])
    def test_manual_mode_persists_and_expires_after_owner_inactivity(self):
        self.assertEqual(HUMAN_IDLE_SECONDS,2400)
        with patch('handoff_store.time.time',return_value=10000):self.worker.handoffs.mode('a','t1','human')
        self.assertEqual(self.worker.handoffs.get('a','t1')['human_until'],12400)
        with patch('handoff_store.time.time',return_value=10100):self.worker.handoffs.renew_human('a','t1')
        self.assertEqual(self.worker.handoffs.get('a','t1')['human_until'],12500)
        Handoffs(self.store);self.assertEqual(self.worker.handoffs.get('a','t1')['human_until'],12500)
        with patch('handoff_store.time.time',return_value=12501):self.assertEqual(len(self.worker.handoffs.expired_humans('a')),1)
    def test_old_two_hour_deadlines_are_migrated_once(self):
        with self.store.db() as db:
            db.execute("DELETE FROM meta WHERE key='human-idle-seconds'")
            db.execute("UPDATE handoffs SET mode='human',human_since=10000,human_until=20000,updated=19000 WHERE thread='t1'")
        Handoffs(self.store);self.assertEqual(self.worker.handoffs.get('a','t1')['human_until'],15200)
        Handoffs(self.store);self.assertEqual(self.worker.handoffs.get('a','t1')['human_until'],15200)
    def test_buttons_switch_persisted_mode_without_changing_the_order(self):
        with self.store.db() as db:
            db.execute("UPDATE handoffs SET group_id=?,topic_id=10,topic_state='ready',control_message_id=100 WHERE thread='t1'",(CONFIG['group_id'],))
            db.execute("INSERT INTO bridge_refs VALUES(?,100,'a','t1','control')",(CONFIG['group_id'],))
        before=self.context()
        def click(action,ident):
            self.bridge.chat_callback({'update_id':ident,'callback_query':{'id':str(ident),'from':{'id':7},
                'message':{'message_id':100,'message_thread_id':10,'chat':{'id':CONFIG['group_id']}},'data':'chat:'+action}})
        click('take',1);route=self.worker.handoffs.get('a','t1');self.assertEqual(route['mode'],'human')
        self.assertAlmostEqual(route['human_until']-time.time(),2400,delta=2)
        click('take',1);self.assertEqual(self.worker.handoffs.get('a','t1')['human_until'],route['human_until'])
        click('bot',2);self.worker.send_human_reply();self.assertEqual(self.worker.handoffs.get('a','t1')['mode'],'bot')
        self.assertEqual(self.context(),before);self.assertEqual(self.client.sent,[])
    def test_expired_manual_mode_is_automatically_returned_to_auto(self):
        self.worker.handoffs.mode('a','t1','human')
        with self.store.db() as db:db.execute("UPDATE handoffs SET human_until=? WHERE thread='t1'",(time.time()-1,))
        self.worker.resume_expired();self.worker.send_human_reply()
        self.assertEqual(self.worker.handoffs.get('a','t1')['mode'],'bot');self.assertEqual(self.client.sent,[])
    def test_existing_topic_is_renamed_in_place(self):
        self.worker.conversation_title('123','102');self.worker.refresh_buyer_names()
        with self.store.db() as db:db.execute("UPDATE handoffs SET group_id=?,topic_id=10,topic_state='ready',telegram_title='old' WHERE thread='t1'",(CONFIG['group_id'],))
        self.bridge.sync_topic_name()
        calls=[fields for method,fields in self.bridge.api.calls if method=='editForumTopic']
        self.assertEqual(len(calls),1);self.assertTrue(calls[0]['name'].startswith('Олена · ID 123'))
        self.assertEqual(calls[0]['message_thread_id'],10);self.assertEqual(self.worker.handoffs.get('a','t1')['mode'],'bot')
    def test_profile_mismatch_does_not_assign_another_buyers_name(self):
        self.client.user=lambda user:{'id':999,'name':'Чужий клієнт'}
        self.worker.conversation_title('123','102');self.worker.refresh_buyer_names()
        self.assertNotIn('Чужий',self.worker.handoffs.get('a','t1')['title'])
        self.assertEqual(self.worker.names.cached('a','123'),'')
    def test_optional_profile_request_has_short_timeout(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"data": {"id": 123}}'
        with patch('app.urlopen',return_value=Response()) as request:
            app.olx_request('/api/partner/users/123',token='test')
            self.assertEqual(request.call_args.kwargs['timeout'],5)
            app.olx_request('/api/partner/threads',token='test')
            self.assertEqual(request.call_args.kwargs['timeout'],25)
    def test_cancellation_of_an_olx_purchase_is_not_falsely_confirmed(self):
        self.process('Олх доставкою');self.process('Оформив');self.process('Передумав')
        self.assertIn('потрібно перевірити в OLX',self.client.sent[-1][1])
        self.assertNotIn('скасовано',self.client.sent[-1][1]);self.assertEqual(self.worker.handoffs.get('a','t1')['mode'],'bot')
        self.assertTrue(any('просить скасувати' in row['text'] for row in self.alerts()))
    def test_full_prepayment_warns_about_delivery_deduction_before_payment(self):
        merchant={'enabled':True,'kind':'card','account':'4111111111111111'}
        bot=Dialogue(self.catalog,DemoInventory(),merchant)
        first=bot.plan(['Хочу замовити М чорну'],{},'102');ctx=dict(first.context,**first.on_send)
        full=bot.plan(['Повна передоплата'],ctx,'102');self.assertIn(FULL_PREPAYMENT_REFUND,full.text)
        self.assertLess(full.text.index(FULL_PREPAYMENT_REFUND),full.text.index('Після оплати надішліть'))
        fullctx=dict(full.context,**full.on_send)
        repeat=bot.plan(['Скиньте реквізити'],fullctx,'102');self.assertIn(FULL_PREPAYMENT_REFUND,repeat.text)
        faq=bot.plan(['А якщо відмовлюся?'],fullctx,'102');self.assertIn(FULL_PREPAYMENT_REFUND,faq.text)
        cod=bot.plan(['Післяплата'],ctx,'102');self.assertNotIn(FULL_PREPAYMENT_REFUND,cod.text)
    def test_cod_deposit_is_explicitly_nonrefundable_on_refusal(self):
        bot=Dialogue(self.catalog,DemoInventory(),{'enabled':True,'kind':'card','account':'4111111111111111'})
        first=bot.plan(['Хочу замовити М чорну'],{},'102');ctx=dict(first.context,**first.on_send)
        cod=bot.plan(['Післяплата'],ctx,'102');self.assertIn(COD_DEPOSIT_REFUND,cod.text)
        self.assertLess(cod.text.index(COD_DEPOSIT_REFUND),cod.text.index('Після оплати надішліть'))
        ctx=dict(cod.context,**cod.on_send)
        for text in ('Скиньте реквізити','Якщо не заберу, передоплата повертається?'):
            answer=bot.plan([text],ctx,'102');self.assertIn(COD_DEPOSIT_REFUND,answer.text)
            self.assertNotIn(FULL_PREPAYMENT_REFUND,answer.text)
    def test_explanation_of_100_hryvnia_is_contextual_and_preserves_invoice(self):
        bot=Dialogue(self.catalog,DemoInventory(),{'enabled':True,'kind':'card','account':'4111111111111111'})
        first=bot.plan(['Хочу замовити М чорну'],{},'102');ctx=dict(first.context,**first.on_send)
        cod=bot.plan(['Післяплата'],ctx,'102');ctx=dict(cod.context,**cod.on_send)
        for text in ('Для чого передоплата 100 грн?','Навіщо берете 100?','А предоплата зачем?','Чому потрібна передоплата?','Для чего 100 гривен?','А навіщо?'):
            old=copy.deepcopy(ctx);p=bot.plan([text],ctx,'102')
            self.assertIn('покрити зворотну доставку',p.text);self.assertIn('входить у вартість товару',p.text)
            self.assertFalse(p.on_send.get('handoff_now'));self.assertEqual(ctx,old);self.assertEqual(p.context['invoice_id'],old['invoice_id'])
    def test_request_without_prepayment_explains_why_and_creates_no_invoice(self):
        for text in ('Можна без передоплати?','Наложкою без 100 грн можна?','Отправите без предоплаты?','Без пп можна?','Не хочу робити передоплату'):
            p=self.worker.dialogue.plan([text],{},'102')
            self.assertIn('Без передоплати 100 грн',p.text);self.assertIn('не відправляємо',p.text)
            self.assertIn('зворотну доставку',p.text);self.assertFalse(p.on_send.get('invoice_issued'));self.assertFalse(p.on_send.get('handoff_now'))
        self.assertIsNone(cod_deposit_question('Я не прошу без передоплати'))
    def test_iban_is_used_for_both_payment_methods_with_correct_terms(self):
        bban='300001'+'0'*18+'1'
        iban='UA'+str(98-int(bban+'301000')%97).zfill(2)+bban
        self.assertTrue(valid_iban(iban));self.assertFalse(valid_iban(iban[:-1]+'2'))
        private=self.path/'iban-settings';private.mkdir()
        (private/'payments.json').write_text(json.dumps({'enabled':True,'kind':'iban','account':iban}))
        merchant=payment_settings(private);bot=Dialogue(self.catalog,DemoInventory(),merchant)
        first=bot.plan(['Хочу замовити М чорну'],{},'102');ctx=dict(first.context,**first.on_send)
        for method,terms,amount in (('Післяплата',COD_DEPOSIT_REFUND,'100'),('Повна передоплата',FULL_PREPAYMENT_REFUND,'650')):
            p=bot.plan([method],ctx,'102');self.assertIn('Рахунок (IBAN)',p.text);self.assertIn(iban,p.text)
            self.assertIn(terms,p.text);self.assertIn('Сума: '+amount+' грн.',p.text);self.assertNotIn('Картка:',p.text)
        (private/'payments.json').write_text(json.dumps({'enabled':True,'kind':'iban','account':iban[:-1]+'2'}))
        with self.assertRaises(AppError):payment_settings(private)

if __name__=='__main__':unittest.main()
