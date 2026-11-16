"""Reported conversations and variants through the dialogue and automatic worker."""
import copy
import json
import unittest

import test_consultation_flow as consultation
import test_sales_journey as journey
from checkout import method_choice, shipping_details
from conversation_policy import requests_owner
from customer_language import requested_color

ADDRESS='Коваленко Олег Петрович\n0671234567\nЛьвів, Львівська область\n69 відділення'


class ConversationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.s=consultation.ConsultationTests();self.s.setUp()

    def assert_autonomous(self,plan):
        self.assertFalse(plan.on_send.get('handoff_now'),plan.text)
        self.assertNotIn('щойно звільниться',plan.text)
        self.assertNotIn('передано власнику',plan.text.lower())
        self.assertNotIn('підключу власника',plan.text.lower())

    def test_screenshot_sequence_reaches_payment_after_noise_and_transliterated_color(self):
        for text in ('Хуй','Хочу куні','Хочу','Замовити','M','Блек'):
            plan=self.s.turn(text,'102');self.assert_autonomous(plan)
        self.assertEqual(self.s.context['color'],'BLACK')
        self.assertEqual(self.s.context['size'],'M')
        self.assertTrue(self.s.context['checkout_offered'])
        plan=self.s.turn('Two','102')
        self.assert_autonomous(plan)
        self.assertEqual(self.s.context['checkout_method'],'cod')
        self.assertTrue(self.s.context['invoice_issued'])
        self.assertIn('100 грн',plan.text)

    def test_unknown_messages_never_escalate_after_a_counter(self):
        for i in range(15):
            plan=self.s.turn('незрозумілий запит номер абв')
            self.assert_autonomous(plan)
            self.assertNotIn('owner_requested',plan.on_send)
        plan=self.s.turn('хочу чорні M')
        self.assertTrue(self.s.context['checkout_offered'])
        self.assert_autonomous(plan)

    def test_old_automatic_owner_promise_is_not_replayed(self):
        self.s.context={'greeted':True,'owner_requested':True,'handoff_now':True,'clarifications':12}
        for text in ('хочу','М','блек'):
            plan=self.s.turn(text)
            self.assert_autonomous(plan)
        self.assertFalse(self.s.context.get('owner_requested'))
        self.assertTrue(self.s.context.get('checkout_offered'))

    def test_insults_at_each_checkout_stage_preserve_the_actual_order(self):
        self.s.order();self.s.turn('2')
        for advance in (False,True):
            if advance:
                self.s.turn(ADDRESS);self.s.turn('так')
            for text in ('Я в рот єбав','Хуй','сука','ти лох'):
                before=copy.deepcopy(self.s.context)
                plan=self.s.turn(text)
                self.assert_autonomous(plan)
                self.assertNotIn('ПІБ:',plan.text)
                self.assertNotIn('Номер телефону:',plan.text)
                self.assertEqual(self.s.context['invoice_id'],before['invoice_id'])
                self.assertEqual(self.s.context.get('shipping_details'),before.get('shipping_details'))
                self.assertEqual(self.s.context.get('customer_confirmation'),before.get('customer_confirmation'))
                self.assertFalse(self.s.context.get('payment_verified'))

    def test_profanity_does_not_hide_a_real_purchase_request(self):
        plan=self.s.turn('блять, хочу чорні М')
        self.assertEqual(self.s.context['size'],'M')
        self.assertEqual(self.s.context['color'],'BLACK')
        self.assertTrue(self.s.context['checkout_offered'])
        self.assert_autonomous(plan)
        plan=self.s.turn('сука, післяплатою')
        self.assertEqual(self.s.context['checkout_method'],'cod')
        self.assertTrue(self.s.context['invoice_issued'])

    def test_common_color_spellings_and_negation(self):
        for code,words in {'BLACK':('Блек','блэк','блєк','black','blak'),
                           'WHITE':('вайт','уайт','white'), 'GRAY':('грей','грэй','grey')}.items():
            for word in words:
                self.assertEqual(requested_color(word),code,word)
        self.assertEqual(requested_color('не блек, а вайт'),'WHITE')

    def test_spoken_menu_choices_are_contextual(self):
        for code,words in {'olx':('one','перший','першим','1-й'),
                           'cod':('two','другий','второй','давай другий варіант','two please'),
                           'full':('three','third','третім','давай третій')}.items():
            for word in words:
                self.assertEqual(method_choice(word),code,word)
        self.s.order();self.s.turn('two')
        self.s.turn('ПІБ: Коваленко Олег Петрович\nТелефон: 0671234567\nМісто: Львів')
        before=self.s.context['invoice_id']
        self.s.turn('2')
        self.assertEqual(self.s.context['shipping_details']['branch'],'Відділення №2')
        self.assertEqual(self.s.context['invoice_id'],before)

    def test_explicit_human_requests_work_in_customer_languages(self):
        for text in ('поклич власника','позови оператора','можна менеджера','Хочу поговорити з людиною',
                     'мені потрібна людина','оператор','human please','can I speak to the owner?',
                     'не хочу бота','нехай власник напише','власника поклич'):
            self.assertTrue(requests_owner(text),text)
            plan=self.s.bot.plan([text],{},'101')
            self.assertTrue(plan.on_send.get('handoff_now'),text)
            self.assertEqual(plan.on_send.get('owner_request_reason'),'customer')

    def test_identity_quotes_and_negations_do_not_call_owner(self):
        for text in ('ти бот?','хто твій власник?','власника не треба','не клич власника',
                     'не хочу оператора','поясни що означає «поклич власника»','не хочу бота міняти, хочу чорну M'):
            self.assertFalse(requests_owner(text),text)
            plan=self.s.bot.plan([text],{},'101')
            self.assertFalse(plan.on_send.get('handoff_now'),text)
        plan=self.s.turn('дай контакти власника')
        self.assertIn('@boy_in_white',plan.text)
        self.assert_autonomous(plan)

    def test_last_choice_controls_conflicting_human_requests(self):
        for text in ('не хочу бота, поклич власника','не хочу бота а хочу людину','можна зв’язатися з продавцем?'):
            self.assertTrue(requests_owner(text),text)
        for text in ('поклич власника, ні не треба','хочу оператора, передумав','власник не потрібен, допоможи вибрати M'):
            self.assertFalse(requests_owner(text),text)

    def test_feedback_with_a_real_question_or_order_is_not_only_thanked(self):
        for text in ('все супер, беру чорні M','дякую, хочу замовити чорні M'):
            self.s.context={}
            plan=self.s.turn(text)
            self.assertTrue(self.s.context.get('checkout_offered'),plan.text)
            self.assert_autonomous(plan)
        plan=self.s.turn('все супер, коли відправите?')
        self.assertRegex(plan.text,'Відправка|Постараємося відправити')

    def test_unknown_product_facts_and_distant_size_offer_help_without_inventing(self):
        for text in ('Яка щільність gsm?','Які бірки?','Який розмір на фото?','Фліс є?',
                     'Які заміри XL?','Який розмір підійде на 190 і 80?'):
            plan=self.s.bot.plan([text],{},'102');self.assert_autonomous(plan)
            self.assertNotIn('утеплені легким флісом',plan.text)
        plan=self.s.bot.plan(['Які бірки?'],{},'102')
        self.assertIn('Точних даних',plan.text)

    def test_already_supplied_body_parameters_are_not_requested_again(self):
        self.s.turn('який розмір порадите на зріст 190 і вагу 80?','102')
        plan=self.s.turn('не знаю','102')
        self.assert_autonomous(plan)
        self.assertNotIn('Підкажіть ще вагу',plan.text)
        self.assertNotIn('Напишіть ваш зріст і вагу',plan.text)

    def test_typical_detours_keep_shipping_details_and_do_not_repeat_the_form(self):
        self.s.order();self.s.turn('two')
        self.s.turn('Коваленко Олег Петрович')
        for text in ('не розумію','ну','що робити','ні не знаю','Я в рот єбав'):
            plan=self.s.turn(text)
            self.assert_autonomous(plan)
            self.assertEqual(self.s.context['shipping_details'],{'name':'Коваленко Олег Петрович'})
        self.s.turn('0671234567')
        self.s.turn('Львів, Львівська область')
        plan=self.s.turn('69 відділення')
        self.assertIn('Усе правильно?',plan.text)
        self.assertEqual(self.s.context['shipping_details']['branch'],'Відділення №69')

    def test_conversational_lines_are_not_recipient_data(self):
        for line in ('Я в рот єбав','не розумію','Ти лох','який колір','чому так','ні не знаю'):
            self.assertEqual(shipping_details(line,{},'name'),{},line)
            self.assertEqual(shipping_details(line,{},'place'),{},line)

    def test_complaint_stays_automatic_and_asks_for_relevant_details(self):
        plan=self.s.turn('Отримав футболку, є дірка, хочу повернути')
        self.assert_autonomous(plan)
        self.assertIn('фото',plan.text)
        plan=self.s.turn('Отримав футболку, є дірка, поклич власника')
        self.assertTrue(plan.on_send.get('handoff_now'))

    def test_pending_quantity_change_cannot_create_a_waybill_for_the_old_quantity(self):
        from fulfillment import parcel_payload
        self.s.order();self.s.turn('two');self.s.turn(ADDRESS);self.s.turn('так')
        self.s.context['fulfillment_opt_in']=True
        self.assertIsNotNone(parcel_payload(self.s.catalog,self.s.context,'account','t','buyer','101'))
        self.s.context['quantity_change']={'line':0,'amount':2,'mode':'set','phase':'apply'}
        self.assertIsNone(parcel_payload(self.s.catalog,self.s.context,'account','t','buyer','101'))

    def test_unclear_answer_after_an_image_keeps_the_photo_question_active(self):
        self.s.order();self.s.turn('two')
        self.s.turn('',attachment=True)
        self.assertEqual(self.s.context['awaiting'],'attachment_purpose')
        plan=self.s.turn('не розумію')
        self.assertIn('квитанція',plan.text)
        self.assertEqual(self.s.context['awaiting'],'attachment_purpose')
        self.assertFalse(self.s.context.get('receipt_submitted'))
        self.s.turn('так')
        self.assertTrue(self.s.context['receipt_submitted'])
        self.assertFalse(self.s.context.get('customer_confirmation'))


class WorkerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.s=journey.JourneyTests();self.s.setUp();self.addCleanup(self.s.doCleanups)

    def test_normal_conversation_does_not_create_help_topic_or_owner_notification(self):
        for text in ('абв','абв ще раз','хуй','хочу','М','блек','two','Я в рот єбав'):
            self.s.turn(text)
        route=self.s.worker.handoffs.get('account','t')
        self.assertEqual(route['mode'],'bot');self.assertFalse(route['attention'])
        self.assertFalse(self.s.bridge.create_topic())
        with self.s.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM admin_outbox WHERE event_key LIKE 'needs-owner:%' AND status='pending'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM admin_outbox WHERE event_key LIKE 'feedback:%' AND status='pending'").fetchone()[0],0)

    def test_actual_customer_request_calls_owner_once_and_bot_keeps_answering(self):
        self.s.turn('поклич власника')
        self.assertTrue(self.s.worker.handoffs.get('account','t')['attention'])
        self.assertEqual(self.s.worker.handoffs.get('account','t')['mode'],'bot')
        self.s.turn('Який матеріал?')
        self.assertIn('60%',self.s.client.sent[-1][1])
        self.assertEqual(self.s.worker.handoffs.get('account','t')['mode'],'bot')
        with self.s.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bridge_outbox WHERE kind='attention'").fetchone()[0],1)

    def test_prepared_legacy_automatic_handoff_is_rebuilt_before_sending(self):
        self.s.prepare('абв')
        worker=self.s.worker
        draft=self.s.store.next('account',worker.session,'approved')
        payload=draft['payload'];payload['on_send']['handoff_now']=True
        payload['text']='Підключу власника.'
        with self.s.store.db() as db:
            db.execute('UPDATE drafts SET payload=? WHERE id=?',(json.dumps(payload),draft['id']))
        worker.send_approved()
        self.assertEqual(self.s.client.sent,[])
        worker.build();worker.send_approved()
        self.assertEqual(len(self.s.client.sent),1)
        self.assertNotIn('власник',self.s.client.sent[-1][1])
        self.assertFalse(worker.handoffs.get('account','t')['attention'])

    def test_old_automatic_help_attention_is_removed_without_changing_manual_control(self):
        self.s.turn('Привіт')
        with self.s.store.db() as db:
            db.execute("UPDATE handoffs SET attention=1,topic_state='new' WHERE account='account' AND thread='t'")
            db.execute("DELETE FROM meta WHERE key='customer-request-policy:account'")
        self.s.worker.handoffs.customer_requests_only('account')
        self.assertFalse(self.s.worker.handoffs.get('account','t')['attention'])
        self.assertEqual(self.s.worker.handoffs.get('account','t')['topic_state'],'dormant')
        self.s.worker.handoffs.mode('account','t','human')
        with self.s.store.db() as db:
            db.execute("DELETE FROM meta WHERE key='customer-request-policy:account'")
        self.s.worker.handoffs.customer_requests_only('account')
        self.assertEqual(self.s.worker.handoffs.get('account','t')['mode'],'human')

