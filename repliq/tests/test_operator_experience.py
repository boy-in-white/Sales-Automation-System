"""End-to-end owner workflows. All customers, credentials and APIs are synthetic."""

import copy
import json
import time
import unittest
from unittest.mock import patch

import test_sales_journey as journey
import test_telegram_bridge as relay
import test_monobank as bank
from catalog import Catalog
from chat_messages import ordered
from handoff_store import Handoffs, HUMAN_IDLE_SECONDS
from manual_session import selection_after_manual
from monobank_payments import MonoError, PaymentChecker, scope
from operator_menu import OperatorMenu, bank_status
from telegram_errors import TelegramError


class OperatorJourneyTests(unittest.TestCase):
    def setUp(self):
        self.s = journey.JourneyTests(); self.s.setUp()
        self.addCleanup(self.s.doCleanups)
        if self.s.bridge.history: self.addCleanup(self.s.bridge.history.close)
        self.s.turn('Привіт'); self.s.owner_topic()

    def write(self, text='Зараз допоможу з вибором', update=50, **extra):
        message = {'message_id': 900 + update, 'date': int(time.time()), 'from': {'id': 7},
                   'chat': {'id': -100123}, 'message_thread_id': 10, 'text': text}
        message.update(extra)
        event = {'update_id': update, 'message': message}
        self.s.bridge.update(event)
        return event

    def mode(self):
        return self.s.worker.handoffs.get('account', 't')['mode']

    def test_typing_in_auto_takes_only_this_chat_and_sends_without_commands(self):
        before = len(self.s.client.sent)
        event = self.write()
        self.assertEqual(self.mode(), 'human')
        self.s.worker.send_approved(); self.s.worker.send_human_reply()
        self.assertEqual(len(self.s.client.sent), before + 1)
        self.assertEqual(self.s.client.sent[-1], ('t', 'Зараз допоможу з вибором'))
        self.s.bridge.update(event); self.s.worker.send_human_reply()
        self.assertEqual(len(self.s.client.sent), before + 1)

    def test_duplicate_update_after_return_to_auto_does_not_take_the_chat_again(self):
        event = self.write()
        self.s.worker.send_human_reply()
        self.s.bridge.update(self.s.callback('bot', 70)); self.s.worker.send_human_reply()
        self.assertEqual(self.mode(), 'bot')
        self.s.bridge.update(event)
        self.assertEqual(self.mode(), 'bot')
        self.assertIsNone(self.s.worker.handoffs.next_reply('account'))

    def test_notes_do_not_switch_auto_or_send_anything(self):
        before = len(self.s.client.sent)
        self.write('/note Уточнити заміри')
        self.assertEqual(self.mode(), 'bot')
        self.s.worker.send_human_reply()
        self.assertEqual(len(self.s.client.sent), before)

    def test_take_then_auto_resumes_a_ready_but_unsent_answer_once(self):
        self.s.prepare('Хочу чорні M')
        before = len(self.s.client.sent)
        self.s.bridge.update(self.s.callback('take', 70))
        self.s.worker.send_approved(); self.assertEqual(len(self.s.client.sent), before)
        self.s.bridge.update(self.s.callback('bot', 71)); self.s.worker.send_human_reply()
        self.s.worker.build(); self.s.worker.send_approved()
        self.assertEqual(len(self.s.client.sent), before + 1)
        self.assertIn('M', self.s.client.sent[-1][1])
        self.s.worker.build(); self.s.worker.send_approved()
        self.assertEqual(len(self.s.client.sent), before + 1)

    def test_owner_answer_prevents_resuming_the_same_customer_message(self):
        self.s.prepare('Хочу чорні M')
        self.write('Чорні M є, оформимо')
        self.s.worker.send_human_reply()
        before = len(self.s.client.sent)
        self.s.bridge.update(self.s.callback('bot', 71)); self.s.worker.send_human_reply()
        self.s.worker.build(); self.s.worker.send_approved()
        self.assertEqual(len(self.s.client.sent), before)

    def test_manual_selection_survives_auto_without_copying_seller_claims(self):
        self.s.turn('Мені S')
        self.s.bridge.update(self.s.callback('take', 70))
        self.s.client.incoming('Беру чорні L')
        self.write('Добре, допоможу', update=51)
        self.s.worker.send_human_reply()
        self.s.bridge.update(self.s.callback('bot', 71)); self.s.worker.send_human_reply()
        context = self.s.store.context('account','t','buyer','101',30)
        self.assertEqual((context['size'],context['color']),('L','BLACK'))
        self.assertFalse(context.get('payment_verified'))
        self.assertFalse(context.get('invoice_issued'))

    def test_quick_auto_then_typing_cannot_leave_a_stale_switch_in_front_of_reply(self):
        self.s.bridge.update(self.s.callback('take', 70))
        self.s.bridge.update(self.s.callback('bot', 71))
        self.write('Підключився, відповідаю особисто')
        self.s.worker.send_human_reply()
        self.assertEqual(self.mode(), 'human')
        self.assertEqual(self.s.client.sent[-1][1], 'Підключився, відповідаю особисто')

    def test_take_context_is_bounded_and_not_repeated_on_another_take_click(self):
        self.s.turn('Який матеріал?'); self.s.turn('Які заміри M?')
        self.s.bridge.update(self.s.callback('take', 70))
        with self.s.store.db() as db:
            first = db.execute("SELECT COUNT(*) FROM bridge_outbox WHERE event_key LIKE 'manual-history:context:%'").fetchone()[0]
        self.assertGreater(first, 0); self.assertLessEqual(first, 2)
        self.s.bridge.update(self.s.callback('take', 71))
        with self.s.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bridge_outbox WHERE event_key LIKE 'manual-history:context:%'").fetchone()[0], first)

    def test_auto_finishes_the_queued_owner_message_before_returning_to_bot(self):
        self.write('Дякую, зараз продовжить помічник')
        self.s.bridge.update(self.s.callback('bot', 71))
        self.s.worker.send_human_reply()
        self.assertIn(('t','Дякую, зараз продовжить помічник'), self.s.client.sent)
        self.assertEqual(self.mode(),'returning')
        self.s.worker.send_human_reply()
        self.assertEqual(self.mode(),'bot')

    def test_uncertain_manual_send_blocks_auto_expiry_and_is_not_retried(self):
        self.write()
        def uncertain(thread, text):
            self.s.client.sent.append((thread,text)); raise TimeoutError()
        with patch.object(self.s.client,'send',side_effect=uncertain):
            self.s.worker.send_human_reply(); self.s.worker.send_human_reply()
        self.assertEqual(self.mode(), 'blocked')
        with patch('handoff_store.time.time',return_value=time.time()+HUMAN_IDLE_SECONDS+1):
            self.s.worker.resume_expired()
        self.assertEqual(self.mode(), 'blocked')
        self.assertEqual(sum(body == 'Зараз допоможу з вибором' for _,body in self.s.client.sent), 1)

    def test_owner_photo_is_not_silently_lost_or_sent_as_a_caption(self):
        self.write('', photo=[{'file_id':'fake'}], caption='Фото товару')
        self.assertEqual(self.mode(), 'human')
        self.assertIsNone(self.s.worker.handoffs.next_reply('account'))
        with self.s.store.db() as db:
            self.assertTrue(db.execute("SELECT 1 FROM bridge_outbox WHERE text LIKE 'Вкладення не надіслано%'").fetchone())

    def test_expired_owner_message_has_a_visible_unsent_notice(self):
        self.write()
        with self.s.store.db() as db: db.execute('UPDATE bridge_replies SET created=?', (time.time()-901,))
        self.s.worker.send_human_reply()
        with self.s.store.db() as db:
            self.assertTrue(db.execute("SELECT 1 FROM bridge_outbox WHERE text LIKE 'Повідомлення не надіслано:%'").fetchone())

    def test_temporary_mono_failure_is_quiet_and_persistent_failure_alerts_once(self):
        before = len(self.s.client.sent)
        for when in (10000,10030,10299):
            self.s.worker.bank_health({'state':'retry','scope':'s','code':'401','updated':when})
        with self.s.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM admin_outbox WHERE event_key LIKE 'mono-problem:%'").fetchone()[0],0)
        for when in (10300,10600,12000):
            self.s.worker.bank_health({'state':'retry','scope':'s','code':'401','updated':when})
        with self.s.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM admin_outbox WHERE event_key LIKE 'mono-problem:%'").fetchone()[0],1)
        self.assertEqual(len(self.s.client.sent),before); self.assertEqual(self.mode(),'bot')

    def test_expired_manual_session_answers_only_the_new_unanswered_message(self):
        self.s.bridge.update(self.s.callback('take', 70))
        self.s.client.incoming('Є чорні M?')
        self.s.worker.collect('t',self.s.client.thread('t'))
        before=len(self.s.client.sent)
        with self.s.store.db() as db:
            db.execute("UPDATE handoffs SET human_until=? WHERE thread='t'",(time.time()-1,))
        self.s.worker.next_manual_check=0
        self.s.worker.resume_expired();self.s.worker.build();self.s.worker.send_approved()
        self.assertEqual(self.mode(),'bot')
        self.assertEqual(len(self.s.client.sent),before+1)
        self.assertIn('M',self.s.client.sent[-1][1]);self.assertIn('наявності',self.s.client.sent[-1][1])

    def test_new_manual_reply_extends_only_its_own_deadline(self):
        self.write();first=self.s.worker.handoffs.get('account','t')['human_until']
        with patch('handoff_store.time.time',return_value=time.time()+60):
            self.write('Ще одне уточнення',update=51)
        second=self.s.worker.handoffs.get('account','t')['human_until']
        self.assertAlmostEqual(second-first,60,delta=1)

    def test_reconnect_during_final_automatic_checks_suppresses_the_bot_reply(self):
        self.s.prepare('Чи є чорні M?')
        before=len(self.s.client.sent);identity=self.s.client.identity
        def takeover():
            self.write('Зараз перевірю особисто')
            return identity()
        with patch.object(self.s.client,'identity',side_effect=takeover):
            self.s.worker.send_approved()
        self.assertEqual(len(self.s.client.sent),before)
        self.s.worker.send_human_reply()
        self.assertEqual(self.s.client.sent[-1][1],'Зараз перевірю особисто')

    def test_two_manual_messages_are_sent_in_order_before_auto_resumes(self):
        self.write('Перше повідомлення',update=50);self.write('Друге повідомлення',update=51)
        self.s.bridge.update(self.s.callback('bot',71))
        for _ in range(4):self.s.worker.send_human_reply()
        manual=[body for _,body in self.s.client.sent if body in ('Перше повідомлення','Друге повідомлення')]
        self.assertEqual(manual,['Перше повідомлення','Друге повідомлення']);self.assertEqual(self.mode(),'bot')

    def test_body_parameters_given_to_owner_are_kept_without_a_fake_payment(self):
        self.s.bridge.update(self.s.callback('take',70))
        self.s.client.incoming('мій зріст 1.85 м, вага 78')
        self.write('Дякую за параметри');self.s.worker.send_human_reply()
        self.s.bridge.update(self.s.callback('bot',71));self.s.worker.send_human_reply()
        context=self.s.store.context('account','t','buyer','101',30)
        self.assertEqual((context['height'],context['weight']),(185,78))
        self.assertFalse(context.get('payment_verified'));self.assertFalse(context.get('invoice_issued'))

    def test_interrupted_manual_send_recovers_as_blocked_after_restart(self):
        self.write();item=self.s.worker.handoffs.next_reply('account')
        self.s.worker.handoffs.finish_reply(item['event_key'],'sending')
        self.s.worker.handoffs.recover('account')
        self.assertEqual(self.mode(),'blocked');self.assertIsNone(self.s.worker.handoffs.next_reply('account'))

    def test_interrupted_auto_send_recovers_as_blocked_after_restart(self):
        self.s.prepare('Є чорні M?')
        item=self.s.store.next('account',self.s.worker.session,'approved')
        self.s.store.transition(item['id'],'approved','sending')
        self.s.worker.initialize()
        self.assertEqual(self.mode(),'blocked')

    def test_manual_mode_cannot_rewrite_a_frozen_invoice_on_return(self):
        self.s.turn('Хочу чорні M');self.s.turn('Післяплата')
        before=self.s.store.context('account','t','buyer','101',30)
        self.s.bridge.update(self.s.callback('take',70))
        self.s.client.incoming('Беру білі L')
        self.write('Уточнимо зміну');self.s.worker.send_human_reply()
        self.s.bridge.update(self.s.callback('bot',71));self.s.worker.send_human_reply()
        after=self.s.store.context('account','t','buyer','101',30)
        for key in ('invoice_id','invoice_items','invoice_total','checkout_amount'):
            self.assertEqual(after[key],before[key])

    def test_unanswered_help_request_returns_to_attention_list_after_manual_expiry(self):
        with self.s.store.db() as db:db.execute("UPDATE handoffs SET attention=1 WHERE thread='t'")
        self.s.bridge.update(self.s.callback('take',70))
        with self.s.store.db() as db:db.execute("UPDATE handoffs SET human_until=? WHERE thread='t'",(time.time()-1,))
        self.s.worker.next_manual_check=0;self.s.worker.resume_expired()
        self.assertEqual(self.mode(),'bot')
        self.assertEqual(self.s.worker.handoffs.get('account','t')['attention'],1)

    def test_delivered_owner_reply_resolves_attention_without_waiting_for_auto(self):
        with self.s.store.db() as db:db.execute("UPDATE handoffs SET attention=1 WHERE thread='t'")
        self.write('Відповідь на уточнення');self.s.worker.send_human_reply()
        self.assertEqual(self.s.worker.handoffs.get('account','t')['attention'],0)


class RoutingAndPanelTests(unittest.TestCase):
    def setUp(self):
        self.s=relay.RelayTests();self.s.setUp();self.addCleanup(self.s.doCleanups)
        if self.s.bridge.history: self.addCleanup(self.s.bridge.history.close)

    def test_explicit_say_cannot_reply_to_a_message_from_another_topic(self):
        self.s.bridge.update(self.s.message('/say Відповідь',topic=20,reply=110))
        self.assertIsNone(self.s.routes.next_reply('account'))

    def test_blocked_message_replay_does_not_send_after_someone_resumes(self):
        self.s.routes.mode('account','a','blocked')
        event=self.s.message('Перевірка',reply=None)
        self.s.bridge.update(event)
        self.s.routes.mode('account','a','human')
        self.s.bridge.update(event)
        self.assertIsNone(self.s.routes.next_reply('account'))

    def test_private_menu_uses_olx_name_shows_preview_and_filters_manual_chats(self):
        self.s.routes.mode('account','a','bot')
        self.s.store.save_context('account','a','buyer-a','101',{'product':'product-id'})
        self.s.routes.title('account','a','Олена · ID buyer-a · Футболка')
        self.s.routes.title('account','b','Андрій · ID buyer-b · Штани')
        with self.s.store.db() as db:
            self.s.routes.enqueue(db,'preview','account','a','customer','Клієнт\nЄ чорний колір?')
        menu=OperatorMenu(self.s.routes,self.s.bridge.registry,'account',-100123)
        menu.show(7,'all');menu.show(7,'manual','human')
        with self.s.store.db() as db:
            rows=db.execute("SELECT * FROM admin_outbox WHERE status='pending'").fetchall()
        self.assertEqual(len(rows),1)
        self.assertIn('Андрій',rows[0]['text']);self.assertNotIn('Олена',rows[0]['text'])
        menu.show(7,'all-again')
        with self.s.store.db() as db:
            row=db.execute("SELECT * FROM admin_outbox WHERE status='pending'").fetchone()
        self.assertIn('Є чорний колір?',row['text'])
        self.assertIn('Олена · ID buyer-a',row['text'])

    def test_private_panel_edits_one_message_and_recreates_only_if_deleted(self):
        class Api:
            def __init__(self):self.calls=[];self.deleted=False
            def call(self,method,fields):
                self.calls.append((method,fields))
                if method=='editMessageText':
                    assert 'disable_notification' not in fields
                if method=='editMessageText' and self.deleted:
                    self.deleted=False;raise TelegramError(400,reason='message_missing')
                return {'message_id':fields.get('message_id',800+len(self.calls))}
        api=Api();self.s.bridge.api=api
        menu=OperatorMenu(self.s.routes,self.s.bridge.registry,'account',-100123)
        for key in ('first','second'):
            menu.show(7,key);self.s.bridge.next_private_send=0;self.s.bridge.deliver_private()
        self.assertEqual([m for m,_ in api.calls],['sendMessage','editMessageText'])
        api.deleted=True;menu.show(7,'third');self.s.bridge.next_private_send=0;self.s.bridge.deliver_private()
        self.assertEqual([m for m,_ in api.calls][-2:],['editMessageText','sendMessage'])

    def test_retried_menu_update_does_not_cancel_its_own_panel(self):
        menu=OperatorMenu(self.s.routes,self.s.bridge.registry,'account',-100123)
        menu.show(7,'same');menu.show(7,'same')
        with self.s.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM admin_outbox WHERE status='pending'").fetchone()[0],1)

    def test_opening_a_quiet_customer_creates_controls_and_a_small_context_without_takeover(self):
        self.s.routes.watch('account','c','buyer-c','101',1,'Олена · ID buyer-c · Футболка')
        with self.s.store.db() as db:
            self.s.routes.enqueue(db,'quiet-c','account','c','customer','Клієнт\nМожна замовити білу M?')
            db.execute("UPDATE bridge_outbox SET status='stored' WHERE event_key='quiet-c'")
        token=self.s.routes.link_token(self.s.routes.get('account','c'))
        opened=self.s.routes.request_topic('account',token,'open-test')
        self.assertEqual(opened['mode'],'bot');self.assertEqual(opened['topic_state'],'new')
        class Api:
            def __init__(self):self.calls=[]
            def call(self,method,fields):
                self.calls.append((method,fields))
                return {'message_thread_id':30} if method=='createForumTopic' else {'message_id':900}
        api=Api();self.s.bridge.api=api;self.s.bridge.next_send=0
        self.assertTrue(self.s.bridge.create_topic());self.s.bridge.next_send=0;self.s.bridge.deliver()
        panel=[fields for method,fields in api.calls if method=='sendMessage'][0]
        self.assertEqual(panel['message_thread_id'],30)
        self.assertIn('auto',panel['text'])
        self.assertEqual(sum(map(len,panel['reply_markup']['inline_keyboard'])),2)
        self.assertEqual(self.s.routes.get('account','c')['mode'],'bot')
        with self.s.store.db() as db:
            self.assertTrue(db.execute("SELECT 1 FROM bridge_outbox WHERE thread='c' AND event_key LIKE 'manual-history:open:%'").fetchone())

    def test_returning_to_auto_drops_late_takeover_recap_but_keeps_requested_history(self):
        with self.s.store.db() as db:
            self.s.routes.enqueue(db,'manual-history:context:old','account','a','history','[]')
            self.s.routes.enqueue(db,'manual-history:explicit','account','a','history','[]')
        self.s.routes.mode('account','a','bot')
        with self.s.store.db() as db:
            states={r['event_key']:r['status'] for r in db.execute("SELECT event_key,status FROM bridge_outbox WHERE kind='history'")}
        self.assertEqual(states['manual-history:context:old'],'stored')
        self.assertEqual(states['manual-history:explicit'],'pending')


class MonoRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.s=bank.BankTests();self.s.setUp();self.addCleanup(self.s.doCleanups)

    def test_unmatched_credit_amendment_invalidates_it_before_all_customers_pay(self):
        one,two=self.s.order(),self.s.order()
        self.assertEqual(self.s.ingest(self.s.tx('p1')),0)
        self.assertEqual(self.s.ingest(self.s.tx('p1',5000),self.s.tx('p2')),0)
        self.assertFalse(self.s.paid(one));self.assertFalse(self.s.paid(two))

    def test_conflicting_duplicate_transaction_cannot_approve_an_order_in_either_order(self):
        for reverse in (False,True):
            s=bank.BankTests();s.setUp();self.addCleanup(s.doCleanups)
            row=s.order();items=[s.tx('same'),s.tx('same',5000)]
            self.assertEqual(s.ingest(*(items[::-1] if reverse else items)),0)
            self.assertFalse(s.paid(row))
            self.assertEqual(s.ingest(s.tx('same')),1) # A later consistent statement can resolve it.

    def test_identical_duplicate_counts_as_one_payment_only(self):
        row=self.s.order();transaction=self.s.tx()
        self.assertEqual(self.s.ingest(transaction,copy.deepcopy(transaction)),1)
        self.assertTrue(self.s.paid(row))
        self.assertEqual(self.s.ingest(transaction),0)

    def test_own_transfer_iban_is_case_and_whitespace_insensitive(self):
        row=self.s.order()
        self.assertEqual(self.s.ingest(self.s.tx(counterIban='own-iban\n')),0)
        self.assertFalse(self.s.paid(row))

    def test_retry_health_survives_rate_limit_and_does_not_break_the_checker(self):
        self.s.order();calls=[];health=[]
        class Api:
            def __init__(self,token):pass
            def info(self):calls.append('info');raise MonoError(429,retry_after=1200)
        checker=PaymentChecker(self.s.root,self.s.queue,self.s.stop,client_factory=Api,clock=lambda:self.s.now,health=health.append)
        checker.tick();self.assertEqual(health[-1]['state'],'retry')
        self.s.now+=301;checker.tick()
        self.assertEqual(calls,['info']);self.assertEqual(health[-1]['code'],'429')
        self.assertGreaterEqual(health[-1]['next_check'],self.s.now)

    def test_bank_status_is_local_and_does_not_disclose_credentials(self):
        from chat_store import Store
        store=Store(self.s.root/'local.sqlite3');self.addCleanup(store.close_current)
        store.set_meta('mono-health',{'scope':scope(self.s.settings),'activation_id':self.s.settings['activation_id'],'state':'retry','code':'401','updated':self.s.now,'next_check':self.s.now+300})
        with patch('monobank_payments.MonoClient.get',side_effect=AssertionError('No bank call')):
            text=bank_status(store)
        self.assertIn('Токен не приймається',text)
        self.assertNotIn('test-token',text);self.assertNotIn('dedicated',text)

    def test_telemetry_failure_never_interrupts_actual_matching(self):
        row=self.s.order();self.s.checker.health=lambda _:(_ for _ in ()).throw(OSError())
        self.s.checker.publish_health('ok',self.s.settings,success=True)
        self.assertEqual(self.s.ingest(self.s.tx()),1);self.assertTrue(self.s.paid(row))

    def test_amended_timestamp_before_invoice_revokes_only_automatic_verification(self):
        row=self.s.order();self.s.ingest(self.s.tx())
        self.s.ingest(self.s.tx(at=self.s.now-200))
        self.assertFalse(self.s.paid(row))
        self.assertEqual(self.s.queue.get(row['id'])['payment_source'],'monobank_review')

    def test_changed_explicit_order_reference_cannot_leave_another_order_paid(self):
        one=self.s.order();self.s.ingest(self.s.tx())
        two=self.s.order()
        self.s.ingest(self.s.tx(comment=two['invoice_id']))
        self.assertFalse(self.s.paid(one));self.assertFalse(self.s.paid(two))


if __name__=='__main__':unittest.main()
