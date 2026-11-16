"""Customer-visible regressions and safe response scheduling, without external APIs."""

import copy
import unittest
from unittest.mock import patch

import test_consultation_flow as consultation
import test_sales as sales
from app import AppError
from checkout import shipping_details
from customer_contact import confirms
from customer_language import parameters, requested_size, requested_sizes

ADDRESS = "Коваленко Олег Петрович\n0671234567\nЛьвів, Львівська область\n12"


class ConversationReadinessTests(unittest.TestCase):
    def setUp(self):
        self.s = consultation.ConsultationTests()
        self.s.setUp()

    def submitted(self):
        self.s.order()
        self.s.turn("Післяплата")
        self.s.turn(ADDRESS)
        self.s.turn("так")

    def test_copying_an_empty_address_form_cannot_replace_collected_data(self):
        saved = shipping_details(ADDRESS, {})
        template = "ПІБ: ___\nНомер телефону: ___\nМісто або село, область: ___\nВідділення або поштомат НП, номер: ___"
        self.assertEqual(shipping_details(template, saved, 'place'), saved)
        self.assertEqual(shipping_details("Який номер відділення?", saved, 'branch'), saved)

    def test_refusal_and_unknown_are_not_recipient_names_or_cities(self):
        for text in ("не знаю", "не хочу", "не можу"):
            for awaiting in ('name', 'place'):
                self.assertEqual(shipping_details(text, {}, awaiting), {})

    def test_labelled_pair_and_metres_are_not_confused_with_size_m(self):
        values, errors = parameters('рост вес 170/50', {}, weight_for_fit=False)
        self.assertEqual(values, {'height': 170, 'weight': 50})
        self.assertFalse(errors)
        for text in ('1.85 м 78 кг', 'зріст 1,85 м, вага 78'):
            self.assertIsNone(requested_size(text))
            self.assertEqual(requested_sizes(text), [])
            values, errors = parameters(text, {}, weight_for_fit=False)
            self.assertEqual(values, {'height': 185, 'weight': 78})
            self.assertFalse(errors)

    def test_measurement_question_cannot_change_a_selected_size(self):
        self.s.turn('Мені S', '102')
        plan = self.s.turn('Які заміри M?', '102')
        self.assertIn('65', plan.text)
        self.assertEqual(self.s.context['size'], 'S')

    def test_two_size_measurement_question_does_not_select_last_size(self):
        self.s.turn('Мені S')
        plan = self.s.turn('Добрий вечір, є L-XL? Скиньте заміри, будь ласка')
        self.assertIn('108', plan.text)
        self.assertIn('110', plan.text)
        self.assertEqual(self.s.context['size'], 'S')

    def test_identity_with_a_product_question_has_one_greeting_and_truthful_disclosure(self):
        plan = self.s.turn('Привіт, ти бот? Який матеріал у штанів?')
        self.assertIn('автоматизований', plan.text)
        self.assertIn('60%', plan.text)
        self.assertLessEqual(plan.text.lower().count('привіт'), 1)
        self.assertNotIn('Добрий день', plan.text)

    def test_stock_question_does_not_edit_a_multi_product_basket(self):
        self.s.turn('Хочу штани M сірі і футболку L білу')
        before = copy.deepcopy(self.s.context['cart'])
        plan = self.s.turn('А є ще чорні футболки L?')
        self.assertIn('наявності', plan.text)
        self.assertEqual(self.s.context['cart'], before)
        self.assertNotIn('handoff_now', plan.on_send)

    def test_stock_question_respects_requested_quantity_without_editing_basket(self):
        self.s.turn('Хочу штани M сірі і футболку L білу')
        before = copy.deepcopy(self.s.context['cart'])
        self.s.stock.values[('gallery-tee', 'L', 'WHITE')] = 1
        plan = self.s.turn('А є 2 білі футболки L?')
        self.assertIn('немає', plan.text)
        self.assertEqual(self.s.context['cart'], before)

    def test_greeting_does_not_discard_an_unpaid_invoice(self):
        self.s.order()
        self.s.turn('Післяплата')
        before = copy.deepcopy(self.s.context)
        self.s.turn('Добрий вечір')
        for key in ('invoice_id', 'invoice_items', 'checkout_amount', 'checkout_stage'):
            self.assertEqual(self.s.context[key], before[key])
        self.assertFalse(self.s.context.get('payment_verified'))

    def test_explicit_new_order_cannot_reuse_a_previous_payment(self):
        self.submitted()
        self.s.context.update(payment_verified=True, verified_invoice_key='old')
        plan = self.s.turn('Хочу нове замовлення')
        self.assertTrue(plan.guard['archive_order'])
        self.assertNotIn('invoice_id', self.s.context)
        self.assertNotIn('payment_verified', self.s.context)
        self.assertNotIn('shipping_details', self.s.context)

    def test_correction_before_label_requires_new_confirmation(self):
        self.submitted()
        old = self.s.context['customer_confirmation']
        plan = self.s.turn('Заміни відділення на 15')
        self.assertEqual(self.s.context['shipping_details']['branch'], 'Відділення №15')
        self.assertFalse(self.s.context.get('customer_confirmation'))
        self.assertIn('Усе правильно?', plan.text)
        self.s.turn('так')
        self.assertNotEqual(self.s.context['customer_confirmation'], old)

    def test_correction_after_label_cannot_silently_change_shipping(self):
        self.submitted()
        before = copy.deepcopy(self.s.context['shipping_details'])
        self.s.context['parcel'] = {'ttn': '20400000000000'}
        plan = self.s.turn('Заміни відділення на 15')
        self.assertEqual(self.s.context['shipping_details'], before)
        self.assertFalse(plan.on_send.get('handoff_now'))
        self.assertIn('накладну', plan.text.lower())

    def test_uncertain_or_correcting_answers_do_not_confirm_an_order(self):
        for text in ('так?', 'так, але інше відділення', 'не впевнений', 'так, номер неправильний', 'ні'):
            self.assertFalse(confirms(text), text)
        for text in ('так, все добре', '👍', '+'):
            self.assertTrue(confirms(text), text)

    def test_photo_or_payment_claim_is_never_bank_confirmation(self):
        self.submitted()
        self.s.turn('Я оплатив')
        self.s.turn('це не квитанція, фото товару', attachment=True)
        self.assertFalse(self.s.context.get('payment_verified'))
        self.assertFalse(self.s.context.get('receipt_submitted'))

    def test_quality_question_uses_only_the_current_product_facts(self):
        plan = self.s.turn('по якості як?', '102')
        self.assertIn('100% бавовна', plan.text)
        self.assertIn('не просвічуються', plan.text)
        self.assertNotIn('фліс', plan.text)
        self.assertNotIn('катишки', plan.text)
        self.assertFalse(plan.on_send.get('handoff_now'))

    def test_customer_asking_where_to_send_details_gets_only_missing_fields(self):
        self.s.order()
        self.s.turn('Післяплата')
        self.s.turn('Коваленко Олег Петрович')
        plan = self.s.turn('а шо писати треба?')
        self.assertIn('Номер телефону:', plan.text)
        self.assertIn('Місто або село, область:', plan.text)
        self.assertIn('Номер відділення:', plan.text)
        self.assertNotIn('ПІБ:', plan.text)
        self.assertEqual(self.s.context['shipping_details'], {'name': 'Коваленко Олег Петрович'})

    def test_olx_customer_is_not_asked_to_duplicate_delivery_contacts(self):
        self.s.order()
        self.s.turn('OLX доставкою')
        plan = self.s.turn('Дані вам казати?')
        self.assertIn('в самому OLX', plan.text)
        self.assertNotIn('ПІБ:', plan.text)
        self.assertFalse(self.s.context.get('invoice_issued'))


class ResponseSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.scene = sales.WorkerTests()
        self.scene.setUp()
        self.addCleanup(self.scene.doCleanups)

    def cycle(self):
        worker = self.scene.worker
        with patch.object(worker, 'send_human_reply'), patch.object(worker, 'parcel_updates'), \
                patch.object(worker, 'send_customer_update'), patch.object(worker, 'refresh_buyer_names'):
            worker.process_cycle()

    def test_ready_reply_is_sent_before_polling_other_chats_even_if_poll_fails(self):
        s = self.scene
        s.approve(s.draft())
        def interrupted_poll():
            self.assertEqual(len(s.client.sent), 1)
            raise AppError('Temporary polling failure', status=503)
        with patch.object(s.worker, 'poll', side_effect=interrupted_poll):
            with self.assertRaises(AppError):
                self.cycle()
        with patch.object(s.worker, 'poll'):
            self.cycle()
        self.assertEqual(len(s.client.sent), 1)

    def test_priority_scheduling_still_rechecks_stock_before_sending(self):
        s = self.scene
        s.approve(s.draft('є чорні M?'))
        s.stock.empty = True
        with patch.object(s.worker, 'poll'):
            self.cycle()
        self.assertEqual(s.client.sent, [])
        pending = s.store.next(s.worker.account, s.worker.session)
        self.assertIn('немає в наявності', pending['payload']['text'])

    def test_priority_scheduling_still_rejects_a_changed_buyer(self):
        s = self.scene
        s.approve(s.draft())
        s.client.buyer = 'other-buyer'
        with patch.object(s.worker, 'poll'):
            self.cycle()
        self.assertEqual(s.client.sent, [])
