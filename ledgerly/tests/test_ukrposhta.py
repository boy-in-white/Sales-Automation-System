"""Offline Ukrposhta contract, batching, accounting and failure boundaries."""

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

import business_db
import tracking_db
import incoming_group_listener as listener
from ukrposhta_api import decode_batch, parcel_snapshot, normalize_ukr_ttn, request_batch, SCRAPER_ID, TrackingUnavailable
from ukrposhta_tracker import UkrTracker
import test_inventory as inventory_tests

ONE='0000000000001'
TWO='0000000000002'


def event(barcode=ONE,code='41000',text='Вручення',at=None):
    return {'barcode':barcode,'event':code,'eventName':text,'step':3,'name':'Тестове відділення',
            'date':datetime.fromtimestamp(at or time.time()-60,timezone.utc).isoformat()}


def response(numbers,code='20800',text='Відправлення',at=None):
    return {'status':'success','data':{'result':{n:{'result':[event(n,code,text,at)]} for n in numbers}}}


class ContractTests(unittest.TestCase):
    def test_created_online_is_not_a_shipped_parcel(self):
        snapshot=parcel_snapshot(ONE,{'result':[event(code='10601',text='Створено онлайн. Очікує приймання')]})
        self.assertEqual(snapshot['status_label'],'Очікує приймання')
        self.assertFalse(snapshot['terminal'])
        unknown=parcel_snapshot(ONE,{'result':[event(code='unknown',text='Дані про відправлення оновлено')]})
        self.assertNotEqual(unknown['status_label'],'Відправлено')

    def test_live_batch_event_array_maps_by_barcode_without_position_assumptions(self):
        sample={'status':'success','data':{'result':[
            event(TWO,code='10601',text='Створено онлайн. Очікує приймання'),
            event(ONE,code='41000',text='Вручено Одержувачу'),
        ],'result_time':'2026-11-09 14:54'}}
        result=decode_batch(sample,[ONE,TWO])
        self.assertEqual(result[ONE]['status_label'],'Отримано')
        self.assertEqual(result[TWO]['status_label'],'Очікує приймання')

    def test_direct_delivery_to_sender_is_not_a_sale(self):
        snapshot=parcel_snapshot(ONE,{'result':[event(code='41000',text='Вручено Відправнику')]})
        self.assertEqual(snapshot['status_label'],'Відмова')
        self.assertTrue(snapshot['terminal'])

    def test_numbers_preserve_leading_zeros_and_reject_nova_cards_and_phone_numbers(self):
        self.assertEqual(normalize_ukr_ttn(ONE),ONE)
        self.assertEqual(normalize_ukr_ttn('rr123456789ua'),'RR123456789UA')
        for value in ('20450000000001','4441111050560535','0671234567','wrong-identifier'):
            self.assertIsNone(normalize_ukr_ttn(value))

    def test_batch_binds_each_event_to_its_barcode(self):
        result=decode_batch(response([ONE,TWO]),[ONE,TWO])
        self.assertEqual(set(result),{ONE,TWO})
        self.assertEqual(result[ONE]['status_label'],'Відправлено')
        wrong={'status':'success','data':{'result':{ONE:{'result':[event(TWO)]}}}}
        with self.assertRaises(TrackingUnavailable):decode_batch(wrong,[ONE])

    def test_empty_or_not_found_never_means_delivered(self):
        for payload in ({'result':[]},{'message':'not found'},{'result':[],'from_to':{'city_to':'Київ'}},
                        {'result':[event()],'date_by_barcode':False}):
            self.assertIsNone(parcel_snapshot(ONE,payload))

    def test_explicit_delivery_is_required(self):
        received=parcel_snapshot(ONE,{'result':[event()]})
        self.assertTrue(received['terminal']);self.assertEqual(received['status_label'],'Отримано')
        for text in ('Передано на вручення','Не вручено','Відправлення не вручено одержувачу'):
            sample=parcel_snapshot(ONE,{'result':[event(code='other',text=text)]})
            self.assertFalse(sample['terminal']);self.assertNotEqual(sample['status_label'],'Отримано')

    def test_return_delivered_to_sender_is_not_a_sale(self):
        now=int(time.time())
        data={'result':[event(code='other',text='Повернення відправнику',at=now-120),event(at=now-60)]}
        snapshot=parcel_snapshot(ONE,data)
        self.assertEqual(snapshot['status_label'],'Відмова');self.assertTrue(snapshot['terminal'])

    def test_events_are_sorted_by_time_and_invalid_dates_do_not_confirm_delivery(self):
        now=int(time.time())
        data={'result':[event(at=now-30),event(code='21700',text='Надходження до відділення зв’язку',at=now-60)]}
        self.assertEqual(parcel_snapshot(ONE,data)['status_label'],'Отримано')
        invalid=event();invalid['date']='not-a-date'
        self.assertIsNone(parcel_snapshot(ONE,{'result':[invalid]}))

    def test_unknown_response_schema_fails_closed(self):
        with self.assertRaises(TrackingUnavailable):
            decode_batch({'status':'success','data':{'result':{'unexpected':['delivered']}}},[ONE])

    def test_http_uses_documented_batch_endpoint_and_keeps_key_in_header(self):
        seen=[]
        class Reply:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self,size):return json.dumps(response([ONE,TWO])).encode()
        class Opener:
            def open(self,request,timeout):seen.append(request);return Reply()
        with patch('ukrposhta_api.build_opener',return_value=Opener()):
            request_batch({'api_key':'fake-secret','scraper_id':SCRAPER_ID},[ONE,TWO])
        request=seen[0]
        self.assertEqual(request.full_url,'https://api.parse.bot/scraper/'+SCRAPER_ID+'/track_multiple_parcels')
        self.assertEqual(request.method,'POST')
        self.assertNotIn('fake-secret',request.full_url)
        self.assertEqual(json.loads(request.data),{'lang':'UA','barcodes':ONE+','+TWO})

    def test_case_of_international_barcode_does_not_change_recipient_binding(self):
        number='RR123456789UA'
        data={'status':'success','data':{'result':{number.lower():{'result':[event(number)]}}}}
        self.assertEqual(decode_batch(data,[number])[number]['status_label'],'Отримано')


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture=inventory_tests.InventoryDatabaseTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.old_url,self.old_path=tracking_db.DATABASE_URL,tracking_db.DB_PATH
        tracking_db.DATABASE_URL=business_db.BUSINESS_DATABASE_URL
        tracking_db.DB_PATH=Path(self.fixture.temp_dir.name)/'inventory.sqlite3'
        self.addCleanup(setattr,tracking_db,'DATABASE_URL',self.old_url)
        self.addCleanup(setattr,tracking_db,'DB_PATH',self.old_path)
        tracking_db.init_db()
        self.bot=inventory_tests.FakeBot()
        self.now=int(time.time())
        self.config={'enabled':True,'api_key':'fake','scraper_id':'fake','monthly_limit':160,'total_limit':180,'budget_id':'test'}
        self.calls=[]
        self.answer=lambda numbers:response(numbers)
        def request(settings,numbers):
            self.calls.append(list(numbers));return self.answer(numbers)
        self.request=request
        self.tracker=UkrTracker(request,self.settings,lambda:self.now)
        self.addCleanup(patch.stopall)
        patch('alerts.notify_admin',return_value=True).start()

    def settings(self):return self.config

    def add(self,number=ONE):
        return tracking_db.upsert_shipment(number,description='Тестовий товар',cost='650',shipment_kind='ОЛХ')

    def test_idle_and_unconfigured_tracker_do_not_call_remote_service(self):
        self.tracker.tick(self.bot);self.assertEqual(self.calls,[])
        self.add();self.config={}
        self.tracker.tick(self.bot);self.assertEqual(self.calls,[])

    def test_confirmed_delivery_updates_ukrposhta_order_and_1c_exactly_once(self):
        self.add();self.answer=lambda numbers:response(numbers,'41000','Вручення')
        self.tracker.tick(self.bot)
        row=tracking_db.get_shipment(ONE)
        self.assertEqual(row['last_status'],'Отримано');self.assertEqual(row['active'],0)
        self.assertGreater(row['onec_sent_at'],0)
        order=business_db.get_order_by_ttn(ONE)
        self.assertEqual(order['carrier'],'Ukrposhta');self.assertEqual(order['status'],'DELIVERED')
        self.assertTrue(any('📮 Укрпошта' in sent.text for sent in self.bot.sent))
        before=len(self.bot.sent)
        self.now+=21600;self.tracker.tick(self.bot)
        self.assertEqual(len(self.bot.sent),before);self.assertEqual(len(self.calls),1)

    def test_100_active_shipments_cost_four_batches_then_sleep_a_day(self):
        for index in range(1,101):self.add('%013d'%index)
        self.answer=lambda numbers:{'status':'success','data':{'result':{n:{'result':[]} for n in numbers}}}
        for _ in range(4):self.tracker.tick(self.bot);self.now+=30
        self.assertEqual([len(batch) for batch in self.calls],[30,30,30,10])
        self.assertEqual(len({n for batch in self.calls for n in batch}),100)
        self.now+=21600;self.tracker.tick(self.bot)
        self.assertEqual(len(self.calls),4)

    def test_partial_not_found_does_not_deliver_another_customer(self):
        self.add(ONE);self.add(TWO)
        self.answer=lambda numbers:response([ONE],'41000','Вручення')
        self.tracker.tick(self.bot)
        self.assertEqual(tracking_db.get_shipment(ONE)['last_status'],'Отримано')
        self.assertNotEqual(tracking_db.get_shipment(TWO)['last_status'],'Отримано')
        self.assertEqual(tracking_db.get_shipment(TWO)['onec_sent_at'],0)

    def test_credit_caps_survive_restart_and_no_topup_is_attempted(self):
        self.add();self.config['total_limit']=1
        self.tracker.tick(self.bot)
        self.now+=21601
        other=UkrTracker(self.request,self.settings,lambda:self.now)
        other.tick(self.bot)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(other.error,'free_credit_limit_reached')

    def test_failed_request_uses_backoff_and_preserves_last_status(self):
        self.add();tracking_db.update_shipment(ONE,last_status='Прибув у відділення')
        def fail(numbers):raise TrackingUnavailable('network')
        self.answer=fail;self.tracker.tick(self.bot)
        self.now+=30
        restarted=UkrTracker(self.request,self.settings,lambda:self.now)
        restarted.tick(self.bot)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(tracking_db.get_shipment(ONE)['last_status'],'Прибув у відділення')
        self.now+=900;self.answer=lambda numbers:response(numbers)
        restarted.tick(self.bot);self.assertEqual(len(self.calls),2)

    def test_telegram_failure_retries_from_cache_without_another_paid_call(self):
        self.add();self.answer=lambda numbers:response(numbers,'41000','Вручення')
        original=self.bot.send_message
        self.bot.send_message=lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('fake Telegram outage'))
        self.tracker.tick(self.bot)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(tracking_db.get_shipment(ONE)['onec_sent_at'],0)
        self.bot.send_message=original;self.now+=301
        restarted=UkrTracker(self.request,self.settings,lambda:self.now)
        restarted.tick(self.bot)
        self.assertGreater(tracking_db.get_shipment(ONE)['onec_sent_at'],0)
        self.assertEqual(len(self.calls),1)

    def test_return_card_failure_is_retried_without_another_paid_call(self):
        self.add();self.answer=lambda numbers:response(numbers,'other','Повернення відправнику')
        original=self.bot.send_message
        failed=[False]
        def fail_first(*args,**kwargs):
            if not failed[0]:
                failed[0]=True
                raise RuntimeError('fake Telegram outage')
            return original(*args,**kwargs)
        self.bot.send_message=fail_first
        self.tracker.tick(self.bot)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(tracking_db.get_shipment(ONE)['active'],1)
        self.bot.send_message=original;self.now+=301
        self.tracker.tick(self.bot)
        self.assertEqual(len(self.calls),1)
        self.assertNotEqual(
            tracking_db.get_shipment(ONE)['last_error'],
            'telegram_notification_pending',
        )
        self.assertTrue(any('Повернення' in sent.text for sent in self.bot.sent))

    def test_orphaned_cached_status_is_retired(self):
        self.tracker.initialize()
        snapshot={'status_label':'Відправлено','status_code':'20800','hash':'x',
                  'profile_name':'ukrposhta','terminal':False,'event_at':self.now,
                  'carrier':'Ukrposhta','place':''}
        with tracking_db._LOCK,tracking_db._transaction() as conn:
            conn.execute('INSERT INTO ukr_tracking_cache(ttn,snapshot,pending,next_retry) VALUES(?,?,1,0)',
                         (ONE,json.dumps(snapshot)))
        self.tracker.deliver_cached(self.bot)
        with tracking_db._LOCK,tracking_db._transaction() as conn:
            row=conn.one('SELECT pending FROM ukr_tracking_cache WHERE ttn=?',(ONE,))
        self.assertEqual(row['pending'],0)

    def test_stale_event_does_not_regress_latest_status(self):
        self.add()
        self.answer=lambda numbers:response(numbers,'21700','Надходження до відділення зв’язку',self.now-60)
        self.tracker.tick(self.bot)
        self.now+=21601
        self.answer=lambda numbers:response(numbers,'10100','Приймання',self.now-86400)
        self.tracker.tick(self.bot)
        self.assertEqual(tracking_db.get_shipment(ONE)['last_status'],'Прибув у відділення')

    def test_historic_delivery_does_not_book_a_new_order_as_received(self):
        self.add()
        self.answer=lambda numbers:response(numbers,'41000','Вручення',self.now-100*86400)
        self.tracker.tick(self.bot)
        row=tracking_db.get_shipment(ONE)
        self.assertEqual(row['received_at'],0);self.assertEqual(row['onec_sent_at'],0)
        self.assertEqual(row['last_error'],'ukr_event_too_old_for_order')
        # A manual refresh uses the same cached event and must not bypass the guard.
        from tracking_service import refresh_ttn
        refresh_ttn(self.bot,ONE)
        current=tracking_db.get_shipment(ONE)
        self.assertEqual(current['received_at'],0)
        self.assertEqual(current['onec_sent_at'],0)
        order=business_db.get_order_by_ttn(ONE)
        self.assertTrue(order is None or order['status'] != 'DELIVERED')

    def test_nova_worker_does_not_claim_ukrposhta_numbers(self):
        self.add();self.add('20450000000001')
        tracking_db.update_shipment(ONE,next_check_at=0)
        tracking_db.update_shipment('20450000000001',next_check_at=0)
        rows=tracking_db.claim_due_shipments(nova_only=True)
        self.assertEqual([r['ttn'] for r in rows],['20450000000001'])

    def test_incoming_ukrposhta_is_queued_for_auto_tracking_not_manual_topic(self):
        message=SimpleNamespace(text='Укрпошта\nТестовий товар\n650 грн\n'+ONE,
                                chat=SimpleNamespace(id=-1001),message_id=1,message_thread_id=10,
                                from_user=SimpleNamespace(id=7,full_name='Тест',username='test'))
        with patch.object(listener,'ukr_tracking_configured',return_value=True):
            self.assertEqual(listener._process_claimed_message(self.bot,message),'processed')
        row=tracking_db.get_shipment(ONE)
        self.assertEqual(row['active'],1)
        self.assertEqual(row['last_status'],'Очікує даних трекінгу')
        self.assertGreater(row['tracking_message_id'],0)
        import tracking_service
        self.assertEqual(row['tracking_thread_id'],tracking_service.TG_TRACKING_THREAD_ID)

    def test_pending_acceptance_does_not_mark_order_as_shipped(self):
        self.add()
        self.answer=lambda numbers:response(numbers,'10601','Створено онлайн. Очікує приймання')
        self.tracker.tick(self.bot)
        self.assertEqual(tracking_db.get_shipment(ONE)['last_status'],'Очікує приймання')
        self.assertNotEqual(business_db.get_order_by_ttn(ONE)['status'],'SHIPPED')
        self.assertEqual(tracking_db.get_shipment(ONE)['onec_sent_at'],0)


if __name__=='__main__':unittest.main()
