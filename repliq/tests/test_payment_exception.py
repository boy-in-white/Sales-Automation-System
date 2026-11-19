import unittest
from types import SimpleNamespace
from payment_exception import plan, consent, valid_draft, TTL_SECONDS
import copy
from test_consultation_flow import ConsultationTests


class ExceptionTests(unittest.TestCase):
    def test_limit_is_rechecked_when_customer_agrees(self):
        saved = {'checkout_offered': True}
        stock = SimpleNamespace(monthly_refusals=lambda: 1)
        offer = plan(stock, 'На жаль зараз на карті не маю', saved)
        self.assertTrue(offer['on_send']['return_payment_exception_pending'])
        saved.update(offer['on_send'])
        stock.monthly_refusals = lambda: 2
        denied = plan(stock, 'так, оплачу повернення', saved)
        self.assertFalse(denied['on_send']['return_payment_exception_pending'])
        self.assertFalse(denied['on_send'].get('return_payment_exception_agreed'))

    def test_agreement_and_database_failure(self):
        saved = {'checkout_offered': True}
        offer = plan(SimpleNamespace(monthly_refusals=lambda: 0), 'немає грошей', saved)
        saved.update(offer['on_send'])
        accepted = plan(SimpleNamespace(monthly_refusals=lambda: 0), 'Згоден', saved)
        self.assertTrue(accepted['on_send']['return_payment_exception_agreed'])
        failed = plan(object(), 'Згоден', saved)
        self.assertFalse(failed['on_send'].get('return_payment_exception_agreed'))

    def test_conditional_questions_and_negation_are_not_consent(self):
        for text in ('не згоден', 'так, але не оплачу', 'оплачу, якщо підійдуть',
                     'не впевнений що оплачу', 'хто оплачує повернення?',
                     'а якщо не оплачу повернення?', 'оплачу потім', 'згоден?',
                     'так\nні', 'згоден на чорні M'):
            self.assertFalse(consent(text), text)
        for text in ('так', 'Згодна!', 'так, оплачу повернення', 'сплачу за повернення посилки'):
            self.assertTrue(consent(text), text)

    def test_expired_or_different_order_cannot_accept_previous_offer(self):
        stock = SimpleNamespace(monthly_refusals=lambda: 0)
        initial = {'checkout_offered': True, 'product': 'pants', 'size': 'M'}
        offered = plan(stock, 'немає грошей', initial, now=100)
        saved = dict(initial, **offered['on_send'])
        for updated, when in ((dict(saved, size='L'), 101), (saved, 100 + TTL_SECONDS + 1)):
            result = plan(stock, 'так', updated, now=when)
            self.assertEqual(result['guard']['payment_exception'], 'expired')
            self.assertFalse(result['on_send'].get('return_payment_exception_agreed'))

    def test_cancel_change_and_detour_clear_pending_consent(self):
        for text in ('скасуй замовлення', 'передумав, хочу чорні L', 'яка тканина?', 'згоден?'):
            s = ConsultationTests(); s.setUp()
            s.stock.monthly_refusals = lambda: 0
            s.order()
            s.turn('На карті зараз немає грошей')
            self.assertTrue(s.context['return_payment_exception_pending'])
            s.turn(text)
            self.assertFalse(s.context.get('return_payment_exception_pending'), text)
            self.assertFalse(s.context.get('return_payment_exception_agreed'), text)

    def test_send_guard_rechecks_count(self):
        stock = SimpleNamespace(monthly_refusals=lambda: 0)
        saved = {'checkout_offered': True}
        offer = plan(stock, 'немає грошей', saved)
        payload = dict(offer, input_context=copy.deepcopy(saved), texts=['немає грошей'])
        self.assertTrue(valid_draft(stock, payload))
        stock.monthly_refusals = lambda: 2
        self.assertFalse(valid_draft(stock, payload))

    def test_shortage_phrasings_and_return_question_do_not_confirm_payment(self):
        stock = SimpleNamespace(monthly_refusals=lambda: 0)
        for text in ('грошей немає', 'не вистарчає коштів', 'не хватает денег',
                     'на карте денег нет', 'маю тільки готівку'):
            saved = {'checkout_offered': True}
            offered = plan(stock, text, saved)
            self.assertEqual(offered['guard']['payment_exception'], 'offer', text)
            saved.update(offered['on_send'])
            question = plan(stock, 'а скільки коштує повернення?', saved)
            self.assertEqual(question['guard']['payment_exception'], 'clarify')
            self.assertFalse(question['on_send'].get('return_payment_exception_agreed'))
            self.assertNotIn('payment_verified', question['on_send'])

    def test_reverse_body_labels_and_measurements(self):
        s = ConsultationTests(); s.setUp()
        answer = s.turn('Чи підійде М на 180 ріст і вагу 80', '102')
        self.assertEqual(s.context['height'], 180)
        self.assertEqual(s.context['weight'], 80)
        self.assertNotIn('Підкажіть ще, будь ласка, ваш зріст', answer.text)


class ExceptionWorkerTests(unittest.TestCase):
    def setUp(self):
        from test_sales_journey import JourneyTests
        self.s = JourneyTests(); self.s.setUp()
        self.addCleanup(self.s.doCleanups)
        self.s.stock.monthly_refusals = lambda: 0

    def test_exception_is_sent_once_and_owner_is_notified(self):
        s = self.s
        s.turn('Хочу замовити чорні M')
        self.assertIn('Погоджуєтесь', s.turn('На карті немає грошей'))
        self.assertIn('Домовились', s.turn('Так, оплачу повернення'))
        context = s.store.context('account','t','buyer','101',30)
        self.assertTrue(context['return_payment_exception_agreed'])
        self.assertFalse(context.get('payment_verified'))
        self.assertEqual(context['owner_request_reason'], 'payment_exception')
        self.assertTrue(s.worker.handoffs.get('account','t')['attention'])
        before = len(s.client.sent)
        s.worker.send_approved()
        self.assertEqual(len(s.client.sent), before)

    def test_second_refusal_between_draft_and_send_rebuilds_offer(self):
        s = self.s
        s.turn('Хочу замовити чорні M')
        s.prepare('На карті немає грошей')
        before = len(s.client.sent)
        s.stock.monthly_refusals = lambda: 2
        s.worker.send_approved()
        self.assertEqual(len(s.client.sent), before)
        s.worker.build(); s.worker.send_approved()
        self.assertIn('недоступне', s.client.sent[-1][1])

    def test_split_height_and_units_keep_gray_order(self):
        for body in [('185', '65'), ('185 см 65 кг',), ('Ріст 185 вага 65',)]:
            s = ConsultationTests(); s.setUp()
            for text in ('Добрий вечір', 'Хочу замовити', 'Штани', 'Колір сірий') + body:
                answer = s.turn(text)
            self.assertEqual(s.context['color'], 'GRAY')
            self.assertEqual(s.context['height'], 185)
            self.assertEqual(s.context['weight'], 65)
            self.assertEqual(s.context['size'], 'L')
            self.assertNotIn('handoff_now', answer.on_send)
