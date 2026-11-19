"""Anonymized regressions from the November 18 conversation audit."""
import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
import test_consultation_flow as consultation
import test_sales as sales
from inventory import stock_rows, StockUnavailable
from size_fulfillment import allocate
from nova_shipping import contents_description
from order_summary import snapshot, summary

class RecentDialogueAudit(unittest.TestCase):
    def setUp(self):
        self.s=consultation.ConsultationTests(); self.s.setUp()

    def test_measurements_followed_by_short_request_not_color_loop(self):
        self.s.turn('Мені потрібні заміри S і заміри M')
        self.s.turn('Треба L')
        reply=self.s.turn('Хочу розміри')
        self.assertIn('108',reply.text)
        self.assertNotIn('Який колір',reply.text)
        self.assertEqual(self.s.context['size'],'L')

    def test_measurement_rejection_offers_next_comparison_not_all_sizes(self):
        self.s.turn('Добрий день')
        self.s.turn('Так')
        self.s.turn('Треба S або M')
        self.s.turn('Мені потрібні заміри S і M')
        reply=self.s.turn('Ці замалі')
        self.assertIn('108',reply.text)
        self.assertNotIn('Зараз є розміри',reply.text)
        self.assertFalse(self.s.context.get('invoice_issued'))

    def test_several_messages_are_one_coherent_size_choice(self):
        self.s.turn('Мені потрібні заміри S і M')
        reply=self.s.bot.plan(['Ці замалі','Треба L','Чорні хочу'],self.s.context,'101')
        current=dict(reply.context,**reply.on_send)
        self.assertEqual((current['size'],current['color']),('L','BLACK'))
        self.assertNotIn('Який розмір',reply.text)
        self.assertNotIn('Який колір',reply.text)

    def test_later_bubble_corrects_choice_without_adding_second_item(self):
        reply=self.s.bot.plan(['Хочу чорні M','Ні, краще білі L'],{},'101')
        current=dict(reply.context,**reply.on_send)
        self.assertEqual((current['size'],current['color']),('L','WHITE'))
        self.assertFalse(current.get('cart'))

    def test_ui_jobs_claimed_once_and_read_only_navigation_skips_slow_jobs(self):
        import tempfile
        from pathlib import Path
        from chat_store import Store
        from product_registry import Registry
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'test.sqlite3')
            self.addCleanup(store.close_current)
            registry=Registry(store,'account')
            registry.job(7,'heavy','add_ad',{'argument':'101'})
            registry.job(7,'quick','sizing_list',{})
            job=registry.next_job(allowed=['sizing_list'],claim=True)
            self.assertEqual(job['event_key'],'quick')
            self.assertIsNone(registry.next_job(allowed=['sizing_list'],claim=True))
            self.assertEqual(registry.next_job()['event_key'],'heavy')

    def test_per_product_toggle_persists_without_changing_other_profiles(self):
        from test_product_sizing_admin import SizingAdminTests
        fixture=SizingAdminTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.job('sizing_substitution',product_id='shirt1',enabled=True,revision=0)
        self.assertTrue(fixture.registry.sizing_profile('shirt1')[1]['size_substitution_enabled'])
        self.assertFalse(fixture.registry.sizing_profile('shirt2')[1].get('size_substitution_enabled',False))
        fixture.job('sizing_substitution',product_id='shirt1',enabled=False,revision=1)
        self.assertFalse(fixture.registry.sizing_profile('shirt1')[1]['size_substitution_enabled'])

    def test_telegram_editor_is_processed_without_olx_or_inventory_requests(self):
        from test_product_sizing_admin import SizingAdminTests
        from telegram_bridge import Bridge
        fixture=SizingAdminTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.worker.telegram_config={'owner_ids':[7]}
        fixture.registry.job(7,'slow-job','add_ad',{'argument':'101'})
        fixture.registry.job(7,'editor','sizing_open',{'product_id':'shirt1'})
        bridge=SimpleNamespace(admin_worker=fixture.worker,registry=fixture.registry)
        Bridge.fast_admin(bridge)
        with fixture.store.db() as db:
            self.assertEqual(db.execute("SELECT status FROM admin_jobs WHERE event_key='editor'").fetchone()[0],'done')
            self.assertEqual(db.execute("SELECT status FROM admin_jobs WHERE event_key='slow-job'").fetchone()[0],'queued')
        self.assertTrue(fixture.outbox())

    def enable(self):
        product=self.s.catalog.products['suvene-pants']
        product['size_substitution_enabled']=True
        for size in ('S','M','L','XL'):
            for color in ('BLACK','WHITE','GRAY'):
                self.s.stock.values['suvene-pants',size,color]=0
        return product

    def test_adjacent_lower_stock_is_available_in_requested_size(self):
        p=self.enable()
        for requested,actual in [('M','S'),('L','M'),('XL','L')]:
            with self.subTest(requested=requested):
                for key in self.s.stock.values:self.s.stock.values[key]=0
                self.s.stock.values['suvene-pants',actual,'BLACK']=3
                rows=stock_rows(self.s.stock,p)
                self.assertTrue(any(r['size_code']==requested and r['color_code']=='BLACK' and r['available']==3 for r in rows))
                result=allocate(self.s.catalog,self.s.stock,[{'product':p['key'],'size':requested,'color':'BLACK','quantity':2}])
                self.assertEqual(result[0]['size'],requested)
                self.assertEqual(result[0]['stock_size'],actual)

    def test_no_reverse_no_two_steps_and_no_color_swap(self):
        p=self.enable();self.s.stock.values[p['key'],'M','BLACK']=2
        rows=stock_rows(self.s.stock,p)
        for size,color in [('S','BLACK'),('XL','BLACK'),('L','GRAY')]:
            self.assertFalse(any(r['size_code']==size and r['color_code']==color and r['available']>0 for r in rows))

    def test_mapping_disabled_keeps_real_stock_only(self):
        p=self.enable();p['size_substitution_enabled']=False
        self.s.stock.values[p['key'],'S','BLACK']=2
        self.assertFalse(any(r['size_code']=='M' and r['available']>0 for r in stock_rows(self.s.stock,p)))

    def test_exact_size_is_preferred_even_if_lower_has_more(self):
        p=self.enable();self.s.stock.values[p['key'],'M','BLACK']=1;self.s.stock.values[p['key'],'S','BLACK']=5
        item={'product':p['key'],'size':'M','color':'BLACK','quantity':1}
        result=allocate(self.s.catalog,self.s.stock,[item])
        self.assertNotIn('stock_size',result[0])

    def test_same_physical_stock_cannot_supply_two_lines_twice(self):
        p=self.enable();self.s.stock.values[p['key'],'S','BLACK']=1
        with self.assertRaises(StockUnavailable):
            allocate(self.s.catalog,self.s.stock,[{'product':p['key'],'size':s,'color':'BLACK','quantity':1} for s in ('S','M')])

    def test_invoice_operator_and_waybill_keep_distinct_sizes(self):
        p=self.enable();self.s.stock.values[p['key'],'S','BLACK']=3
        self.s.turn('Хочу чорні M')
        self.s.turn('2')
        item=self.s.context['invoice_items'][0]
        self.assertEqual((item['size'],item['stock_size']),('M','S'))
        order=snapshot(self.s.catalog,self.s.context)
        self.assertIn('Замовлено M → відправити S',summary(order,internal=True))
        self.assertNotIn('відправити S',summary(order))
        from fulfillment import parcel_payload
        from order_summary import confirmation_key
        context=dict(self.s.context,fulfillment_opt_in=True,shipping_details={'name':'Коваленко Олег Петрович','phone':'+380671234567','place':'Львів','branch':'69'})
        context['customer_confirmation']=confirmation_key(snapshot(self.s.catalog,context))
        parcel=parcel_payload(self.s.catalog,context,'account','thread','buyer','101')
        self.assertIsNotNone(parcel)
        self.assertEqual((parcel['items'][0]['requested_size'],parcel['items'][0]['size']),('M','S'))
        self.assertEqual(parcel['cod_cents'],70000)
        self.assertEqual(contents_description([{'name':'Штани Suvene','size':'S','requested_size':'M','size_substituted':True,'color':'BLACK'}]),'Штани, M*, чорний')

class IncomingRaceAudit(unittest.TestCase):
    def setUp(self):
        self.s=sales.WorkerTests();self.s.setUp()
        self.addCleanup(self.s.doCleanups)

    def test_incoming_during_send_is_not_lost_behind_bot_reply(self):
        draft=self.s.draft('Добрий день');self.s.approve(draft)
        def racing_send(thread,text):
            self.s.client.incoming('Треба L')
            sent={'uuid':'own-reply','id':len(self.s.client.history)+1,'thread_uuid':'t','type':'sent','text':text,'created_at':datetime.now(timezone.utc).isoformat()}
            self.s.client.history.append(sent)
            return sent
        self.s.client.send=racing_send
        self.s.worker.send_approved()
        self.s.worker.collect('t',self.s.client.thread('t'))
        self.assertEqual(self.s.worker.buffers['t']['texts'],['Треба L'])
        self.s.worker.buffers['t']['due']=0
        self.s.worker.build()
        pending=self.s.store.next(self.s.worker.account,self.s.worker.session)
        self.assertEqual(pending['payload']['context']['size'],'L')

    def test_manual_reply_remains_a_hard_boundary(self):
        from chat_messages import candidate
        history=[{'_id':'a','type':'received','text':'Треба M'}, {'_id':'manual','type':'sent','text':'Відповів особисто'}]
        self.assertIsNone(candidate(history,{'a'},transparent_sent={'different-bot-id'}))
