"""Real checkout failure shapes; synthetic identities, stock and payment data only."""
import copy
import unittest
from unittest.mock import patch
import test_consultation_flow as consultation
from checkout import method_choice, shipping_details
from customer_contact import branch_value
from shipping_directory import ShippingDirectory, details_key

PERSON = 'Коваленко Олег Петрович'
PHONE = '+380671234567'
ADDRESS = PERSON + ', ' + PHONE + ', М.Сокиряни Чернівецька обл., Нова пошта номер 1'
FIELDS = {'name': PERSON, 'phone': PHONE, 'place': 'Сокиряни, Чернівецька обл.', 'branch': 'Відділення №1'}


class Directory(ShippingDirectory):
    def __init__(self):
        self.calls = []

    def client(self):
        return object()

    def read(self, client, method, props):
        self.calls.append((method, props))
        if method == 'getCities':
            if props['FindByString'].casefold() not in ('сокиряни', 'сокир'):
                return []
            return [{'Ref': 'city', 'Description': 'Сокиряни', 'AreaDescription': 'Чернівецька'}]
        return [{'Ref': 'branch-1', 'CityRef': 'city', 'Number': '1',
                 'Description': 'Відділення №1: вул. Шевченка, 5', 'WarehouseStatus': 'Working'}]


class CheckoutRecovery(unittest.TestCase):
    def setUp(self):
        self.s = consultation.ConsultationTests(); self.s.setUp()

    def start(self):
        self.s.order(); self.s.turn('2')

    def test_actual_payment_proposal_then_colloquial_acceptance(self):
        self.s.turn('доброго дня чи можна замовити перед платою розмір М')
        self.s.turn('Чорний')
        self.s.turn('можна перед платою (оплата на пошті)?')
        self.assertEqual(self.s.context.get('pending_checkout_method'), 'cod')
        result = self.s.turn('добре давайте передоплатою')
        self.assertEqual(self.s.context.get('checkout_method'), 'cod')
        self.assertTrue(self.s.context.get('invoice_issued'))
        self.assertIn('100 грн', result.text)
        self.assertNotIn('Який спосіб', result.text)

    def test_pending_cod_acceptance_does_not_issue_multiple_invoices(self):
        for acceptance in ('добре давайте передоплатою', 'давайте перед платою', 'так, цим способом', 'згоден', 'підходить', 'так, давайте'):
            with self.subTest(acceptance=acceptance):
                self.setUp(); self.s.order()
                self.s.turn('Можна оплатити на пошті?')
                self.s.turn(acceptance)
                self.assertEqual(self.s.context.get('checkout_method'), 'cod')
                invoice = self.s.context['invoice_id']
                self.s.turn(acceptance)
                self.assertEqual(self.s.context['invoice_id'], invoice)

    def test_bare_prepayment_without_context_never_guesses_full_or_cod(self):
        self.s.order()
        result = self.s.turn('передоплатою')
        self.assertFalse(self.s.context.get('invoice_issued'))
        self.assertIn('100 грн', result.text)
        self.assertIn('повн', result.text)
        self.assertNotIn('OLX Доставкою', result.text)

    def test_delivery_preference_is_kept_while_choosing_color(self):
        self.s.turn('Хочу замовити M, можна оплатити на пошті?')
        self.assertEqual(self.s.context.get('pending_checkout_method'), 'cod')
        result = self.s.turn('чорний')
        self.assertIn('100 грн', result.text)
        self.assertNotIn('1. OLX', result.text)
        self.s.turn('так')
        self.assertEqual(self.s.context.get('checkout_method'), 'cod')

    def test_negated_method_is_not_counted_as_second_choice(self):
        for phrase, method in (('не хочу OLX, давайте післяплатою', 'cod'),
                               ('не післяплатою, повна передоплата', 'full'),
                               ('не повна оплата, післяплатою', 'cod')):
            self.assertEqual(method_choice(phrase), method)

    def test_explicit_full_payment_overrides_previous_cod_proposal(self):
        self.s.order(); self.s.turn('можна оплатити на пошті?')
        self.s.turn('ні, давайте повну передоплату')
        self.assertEqual(self.s.context.get('checkout_method'), 'full')

    def test_payment_questions_are_not_a_final_method_acceptance(self):
        for query in ('а можна передоплатою?', 'чому 100 грн?', 'без передоплати?', 'не хочу передоплату'):
            with self.subTest(query=query):
                self.setUp(); self.s.order(); self.s.turn('можна оплатити на пошті?')
                self.s.turn(query)
                self.assertFalse(self.s.context.get('invoice_issued'))

    def test_conversation_acknowledgments_are_never_shipping_fields(self):
        phrases = ('добре зараз все зроблю', 'добре зараз', 'Добре, зараз скину',
                   'зараз зроблю', 'секунду', 'хорошо сейчас все сделаю', 'вже сказав',
                   'зачекайте хвилинку', 'одну хвилинку', 'ще шукаю', 'готово',
                   'зараз відправлю', 'почекай', 'зрозумів дякую', 'сейчас заполню')
        for raw in phrases:
            for awaiting in (None, 'name', 'place', 'phone', 'branch'):
                with self.subTest(raw=raw, awaiting=awaiting):
                    self.assertEqual(shipping_details(raw, {}, awaiting), {})
                    self.assertEqual(shipping_details(raw, FIELDS, awaiting), FIELDS)

    def test_customer_asking_for_time_does_not_receive_another_form(self):
        self.start(); previous = copy.deepcopy(self.s.context)
        for phrase in ('добре зараз все зроблю', 'добре зараз', 'зачекайте хвилинку'):
            answer = self.s.turn(phrase)
            self.assertFalse(self.s.context.get('shipping_details'))
            self.assertEqual(self.s.context.get('shipping_attempts', 0), 0)
            self.assertNotIn('Номер телефону:', answer.text)
            self.assertNotIn('ПІБ', answer.text)
            self.assertEqual(self.s.context['invoice_id'], previous['invoice_id'])

    def test_plain_acknowledgment_is_not_an_address_reminder(self):
        self.start()
        for phrase in ('добре', 'окей', 'так'):
            answer = self.s.turn(phrase)
            self.assertIn('на зв’язку', answer.text)
            self.assertFalse(self.s.context.get('shipping_details'))
            self.assertFalse(self.s.context.get('customer_confirmation'))

    def test_nova_poshta_branch_notation_variants(self):
        for value in ('Нова пошта номер 1', 'Нова пошта N*1', 'НП №1', 'НП N1',
                      'НП N°1', 'Нова пошта #1', 'нова пошта № 1', 'НП номер: 1',
                      'відділення номер 1', 'відділення №1', 'відділення No.1',
                      '1 відділення', 'відділення 1 НП', 'Новая почта номер 1'):
            with self.subTest(value=value):
                self.assertEqual(branch_value(value), 'Відділення №1')
                self.assertEqual(shipping_details(value, {}, 'branch'), {'branch': 'Відділення №1'})

    def test_locker_kind_preserved_with_number_marker(self):
        for value in ('поштомат номер 12345', 'поштомат N*12345', 'НП поштомат №12345'):
            with self.subTest(value=value):
                self.assertEqual(branch_value(value), 'Поштомат №12345')

    def test_one_line_address_with_carrier_number_and_unspaced_city_prefix(self):
        details = shipping_details(ADDRESS, {}, 'name')
        self.assertEqual(details, dict(FIELDS, place_type='місто'))

    def test_address_order_is_flexible(self):
        for raw in (ADDRESS, 'Сокиряни, Чернівецька обл.\nНП N*1\n'+PHONE+'\n'+PERSON,
                    PHONE+'\n'+PERSON+'\nСокиряни, Чернівецька обл.\nНП номер 1'):
            with self.subTest(raw=raw):
                details = shipping_details(raw, {}, 'name')
                self.assertEqual(details['name'], PERSON)
                self.assertEqual(details['phone'], PHONE)
                self.assertEqual(details['branch'], 'Відділення №1')
                self.assertIn('Сокиряни', details['place'])

    def test_directory_splits_unpunctuated_city_and_region(self):
        directory = Directory()
        details = dict(FIELDS, place='М.Сокиряни Чернівецька обл.')
        review = directory.review(details)
        self.assertEqual(review['status'], 'ready')
        self.assertEqual(review['details']['warehouse_ref'], 'branch-1')
        self.assertEqual(directory.calls[0][1]['FindByString'], 'Сокиряни')

    def test_saved_legacy_chatter_and_embedded_branch_are_repaired(self):
        bad = {'name': 'добре зараз все зроблю', 'place': 'добре зараз'}
        self.assertEqual(shipping_details('зачекайте', bad, 'phone'), {})
        bad = dict(FIELDS, place='М.Сокиряни Чернівецька обл., Нова пошта номер 1')
        bad.pop('branch')
        repaired = shipping_details('вже сказав', bad, 'branch')
        self.assertEqual(repaired['branch'], 'Відділення №1')
        self.assertNotIn('Нова пошта', repaired['place'])

    def test_full_customer_journey_has_no_repetition_or_unrequested_owner(self):
        self.s.bot.address_book = Directory()
        for raw in ('доброго дня чи можна замовити перед платою розмір М', 'Чорний',
                    'можна перед платою (оплата на пошті)?', 'добре давайте передоплатою',
                    'добре зараз все зроблю', 'добре зараз'):
            result = self.s.turn(raw)
            self.assertFalse(result.on_send.get('handoff_now'))
        invoice = self.s.context['invoice_id']
        result = self.s.turn(ADDRESS)
        self.assertIn('Усе правильно?', result.text)
        self.assertEqual(self.s.context['shipping_details']['warehouse_ref'], 'branch-1')
        self.assertNotIn('Підкажіть лише', result.text)
        result = self.s.turn('так, все вірно')
        self.assertTrue(self.s.context['customer_confirmation'])
        self.assertRegex(result.text, 'Відправка|Постараємося')
        self.assertEqual(self.s.context['invoice_id'], invoice)
        self.assertFalse(self.s.context.get('payment_verified'))

    def test_invalid_phone_is_the_only_field_requested(self):
        self.start()
        result = self.s.turn(ADDRESS.replace(PHONE, '067123456'))
        self.assertEqual(self.s.context['shipping_details']['branch'], 'Відділення №1')
        self.assertIn('номер телефону', result.text.lower())
        self.assertNotIn('ПІБ:', result.text)

    def test_burst_address_is_one_shipment_and_no_field_is_lost(self):
        self.start()
        result = self.s.bot.plan([PERSON, PHONE, 'М.Сокиряни Чернівецька обл.', 'Нова пошта N*1'], self.s.context, '101')
        details = result.context['shipping_details']
        self.assertEqual(details['name'], PERSON)
        self.assertEqual(details['phone'], PHONE)
        self.assertEqual(details['branch'], 'Відділення №1')
        self.assertIn('Усе правильно?', result.text)

    def test_branch_address_is_not_a_city_or_new_name(self):
        known = dict(FIELDS); known.pop('branch')
        self.assertEqual(shipping_details('вулиця Шевченка 5', known, 'branch'), known)

    def test_shipping_corrections_do_not_keep_an_old_confirmation(self):
        self.start(); self.s.turn(ADDRESS); self.s.turn('так')
        self.s.turn('Заміни номер телефону на 0677654321')
        self.assertEqual(self.s.context['shipping_details']['phone'], '+380677654321')
        self.assertFalse(self.s.context.get('customer_confirmation'))

    def test_address_and_question_in_one_burst_are_both_processed(self):
        self.start(); self.s.bot.address_book = Directory()
        result = self.s.bot.plan([ADDRESS, 'А на зиму вони підходять?'], self.s.context, '101')
        details = result.context.get('shipping_details', {})
        self.assertEqual(details.get('name'), PERSON)
        self.assertEqual(details.get('branch'), 'Відділення №1')
        self.assertIn('зиму', result.text)
        self.assertIn('Усе правильно?', result.text)

    def test_partial_address_and_material_question_do_not_lose_either(self):
        self.start()
        result = self.s.bot.plan([PERSON, 'Склад тканини який?'], self.s.context, '101')
        self.assertEqual(result.context.get('shipping_details', {}).get('name'), PERSON)
        self.assertIn('60%', result.text)
        self.assertFalse(result.on_send.get('handoff_now'))

    def test_wait_with_a_real_question_answers_question_without_field_pollution(self):
        self.start()
        result = self.s.turn('Зараз скину. А матеріал який?')
        self.assertIn('60%', result.text)
        self.assertFalse(self.s.context.get('shipping_details'))

    def test_back_to_back_addresses_stay_separate_between_customers(self):
        self.start(); state = copy.deepcopy(self.s.context)
        first = self.s.bot.plan([ADDRESS], state, '101')
        second = self.s.bot.plan(['Петренко Іван Олегович\n0677654321\nКиїв\nНП №2'], state, '101')
        self.assertEqual(first.context['shipping_details']['name'], PERSON)
        self.assertEqual(second.context['shipping_details']['name'], 'Петренко Іван Олегович')
        self.assertEqual(second.context['shipping_details']['branch'], 'Відділення №2')
        self.assertNotIn('shipping_details', state)

    def test_tracking_definition_is_not_a_complaint_or_purchase(self):
        for text in ('а ттн це що, номер заказу?', 'ТТН це номер замовлення?', 'что такое ттн?', 'Що таке накладна?'):
            with self.subTest(text=text):
                self.setUp()
                answer = self.s.turn(text)
                self.assertIn('номер накладної', answer.text)
                self.assertIn('Номер замовлення', answer.text)
                self.assertNotIn('сталося з товаром', answer.text)
                self.assertFalse(self.s.context.get('order_requested'))
                self.assertFalse(self.s.context.get('invoice_issued'))

    def test_question_mark_refers_to_previous_definition_even_after_old_bad_reply(self):
        self.s.context = {'greeted': True, 'product': 'suvene-pants',
                          'conversation_memory': {'turns': [{'customer': 'а ттн це що, номер заказу?', 'answer': 'Що сталося з товаром?', 'topics': []}]}}
        for _ in range(2):
            answer = self.s.turn('?')
            self.assertIn('ТТН — це', answer.text)
            self.assertNotIn('характеристику', answer.text)
            self.assertNotIn('в описі', answer.text)

    def test_arrival_notification_request_does_not_promise_today_or_payment(self):
        self.start(); self.s.turn(ADDRESS); self.s.turn('так')
        self.s.context['parcel'] = {'ttn': '20450000000001'}
        answer = self.s.turn('напишите когда приедет)')
        self.assertIn('20450000000001', answer.text)
        self.assertNotIn('Відправка сьогодні', answer.text)
        self.assertNotIn('характеристику', answer.text)
        self.assertFalse(self.s.context.get('payment_verified'))

    def test_question_mark_reexplains_a_fact_without_changing_checkout(self):
        self.start(); invoice = self.s.context['invoice_id']
        self.s.turn('Який матеріал?')
        answer = self.s.turn('?')
        self.assertIn('60%', answer.text)
        self.assertEqual(self.s.context['invoice_id'], invoice)
        self.assertFalse(self.s.context.get('shipping_details'))

    def test_order_number_question_uses_existing_number_not_complaint(self):
        self.start(); invoice = self.s.context['invoice_id']
        answer = self.s.turn('який номер замовлення?')
        self.assertIn(invoice, answer.text)
        self.assertNotIn('сталося з товаром', answer.text)

    def test_cancelled_label_is_not_reintroduced_as_an_active_parcel(self):
        self.s.context = {'greeted': True, 'parcel': {'ttn': '20450000000001', 'status': 'cancelled'}}
        answer = self.s.turn('напишите когда приедет')
        self.assertIn('скасоване', answer.text)
        self.assertNotIn('сьогодні', answer.text)

    def test_request_without_an_order_does_not_invent_a_parcel(self):
        answer = self.s.turn('напиши коли приїде')
        self.assertFalse(self.s.context.get('invoice_issued'))
        self.assertNotIn('надішлю ТТН', answer.text)

    def test_worker_round_trip_keeps_one_invoice_and_collects_burst(self):
        from test_sales_journey import JourneyTests
        fixture = JourneyTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.turn('Хочу замовити M')
        fixture.turn('Чорний')
        fixture.turn('можна перед платою (оплата на пошті)?')
        fixture.turn('добре давайте передоплатою')
        before = fixture.store.context('account', 't', 'buyer', '101', 30)
        self.assertTrue(before['invoice_issued'])
        answer = fixture.turn('добре зараз все зроблю')
        self.assertIn('на зв’язку', answer)
        fixture.client.incoming(PERSON)
        fixture.client.incoming(PHONE)
        fixture.client.incoming('М.Сокиряни Чернівецька обл.')
        fixture.prepare('Нова пошта N*1')
        fixture.worker.send_approved()
        after = fixture.store.context('account', 't', 'buyer', '101', 30)
        self.assertEqual(after['shipping_details']['name'], PERSON)
        self.assertEqual(after['shipping_details']['branch'], 'Відділення №1')
        self.assertEqual(after['invoice_id'], before['invoice_id'])
        self.assertEqual(fixture.worker.handoffs.get('account', 't')['mode'], 'bot')


if __name__ == '__main__':
    unittest.main()
