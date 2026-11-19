"""Replay buyer wording with fictional identities and local service doubles."""

import time
import unittest

from checkout import shipping_details
from customer_updates import CustomerUpdates
from shipping_directory import ShippingDirectory
import test_carrier_payment as carrier
import test_checkout_recovery as recovery
import test_consultation_flow as consultation
import test_fulfillment as fulfillment


NOTICE = 'як відправите то напишіть якщо зможете'
SHIPMENT_NOTICE = 'Посилку відправлено. Ваша ТТН'


class CityDirectory(ShippingDirectory):
    def __init__(self):
        pass

    def client(self):
        return object()

    def read(self, client, method, props):
        if method == 'getWarehouses':
            return [{'Ref': props['CityRef'] + '-branch', 'CityRef': props['CityRef'],
                     'Number': props['FindByString'], 'WarehouseStatus': 'Working'}]
        if props['FindByString'] == 'Самар':
            return [{'Ref': 'samar', 'Description': 'Самар', 'AreaDescription': 'Дніпропетровська', 'SettlementTypeDescription': 'місто'}]
        return [{'Ref': 'city-lanivtsi', 'Description': 'Ланівці', 'AreaDescription': 'Тернопільська', 'SettlementTypeDescription': 'місто'},
                {'Ref': 'village-lanivtsi', 'Description': 'Ланівці (Тернопільська обл.)', 'AreaDescription': 'Тернопільська', 'SettlementTypeDescription': 'село'}]


class DialogueQualityTests(unittest.TestCase):
    def setUp(self):
        self.session = consultation.ConsultationTests()
        self.session.setUp()

    def order(self):
        self.session.order()
        self.session.turn('2')

    def test_stock_and_specific_payment_question_in_one_turn(self):
        plan = self.session.turn('Доброго вечора, розмір М в наявності? Накладним відправляєте?')
        self.assertIn('М', plan.text.replace('M', 'М'))
        self.assertIn('в наявності', plan.text)
        self.assertIn('післяплатою', plan.text.lower())
        self.assertNotIn('Який варіант', plan.text)
        self.assertNotIn('4111', plan.text)
        self.assertEqual(self.session.context['pending_checkout_method'], 'cod')
        self.assertFalse(self.session.context.get('invoice_issued'))

    def test_questions_about_terms_keep_amount_and_address_state(self):
        self.order()
        invoice = self.session.context['invoice_id']
        plan = self.session.turn('повна сума чи тільки предоплата')
        self.assertIn('100 грн', plan.text)
        self.assertFalse(self.session.context.get('shipping_details'))
        plan = self.session.turn('та чи также сама карта?')
        self.assertIn('реквізити', plan.text)
        self.assertNotIn('4111', plan.text)
        self.assertEqual(self.session.context['invoice_id'], invoice)

    def test_question_about_discount_does_not_select_prepayment(self):
        self.session.order()
        plan = self.session.turn('А можна на 100 грн дешевше?')
        self.assertNotIn('4111', plan.text)
        self.assertFalse(self.session.context.get('invoice_issued'))

    def test_payment_question_without_punctuation_is_not_acceptance(self):
        self.session.order()
        plan = self.session.turn('накладним відправляєте')
        self.assertIn('післяплатою', plan.text.lower())
        self.assertFalse(self.session.context.get('invoice_issued'))
        self.session.turn('Добре давайте')
        self.assertTrue(self.session.context['invoice_issued'])

    def test_repeated_payment_number_cannot_replace_address_or_invoice(self):
        self.order()
        self.session.turn(recovery.ADDRESS)
        invoice = self.session.context['invoice_id']
        self.session.turn('2')
        self.assertEqual(self.session.context['shipping_details']['branch'], 'Відділення №1')
        self.session.turn('так')
        plan = self.session.turn('2')
        self.assertEqual(self.session.context['invoice_id'], invoice)
        self.assertNotIn('4111', plan.text)
        self.assertTrue(self.session.context.get('customer_confirmation'))

    def test_contact_text_with_region_first_and_explicit_branch(self):
        fields = shipping_details(
            f'{recovery.PERSON}, {recovery.PHONE}, Тернопільська область місто Ланівці. '
            'Відділення нової пошти номер 1', {}, 'name')
        self.assertEqual(fields, dict(recovery.FIELDS, place='Ланівці, Тернопільська область', place_type='місто'))

    def test_carrier_first_and_genitive_region_with_street_number(self):
        raw = f'НП №7 м. Самар Дніпропетровської області, вул.Гідності 29Д; {recovery.PHONE}; {recovery.PERSON}'
        fields = shipping_details(raw, {}, 'name')
        self.assertEqual(fields, dict(recovery.FIELDS, place='Самар, Дніпропетровської області', branch='Відділення №7', place_type='місто'))
        reviewed = CityDirectory().review(fields)
        self.assertEqual(reviewed['status'], 'ready')
        self.assertEqual(reviewed['details']['warehouse_ref'], 'samar-branch')

    def test_labeled_contact_fields_without_newlines(self):
        raw = f'Телефон: {recovery.PHONE} Місто: Хмельницький поштове відділення номер 28 ПІБ: {recovery.PERSON}'
        self.assertEqual(shipping_details(raw, {}, 'name'),
                         dict(recovery.FIELDS, place='Хмельницький', branch='Відділення №28', place_type='місто'))

    def test_city_and_village_hints_survive_separate_messages(self):
        for spelling, reference in (('Місто: Ланівці', 'city-lanivtsi'), ('с. Ланівці', 'village-lanivtsi')):
            with self.subTest(spelling=spelling):
                fields = shipping_details(spelling, {'name': recovery.PERSON, 'phone': recovery.PHONE}, 'place')
                fields = shipping_details('НП №1', fields, 'branch')
                result = CityDirectory().review(fields)
                self.assertEqual(result['status'], 'ready')
                self.assertEqual(result['details']['city_ref'], reference)
        result = CityDirectory().review(dict(recovery.FIELDS, place='Ланівці, Тернопільська область'))
        self.assertEqual(result['status'], 'choice')
        self.assertIn('місто Ланівці', result['prompt'])
        self.assertIn('село Ланівці', result['prompt'])

    def test_contact_receipt_and_product_question_are_preserved_together(self):
        self.order()
        self.session.bot.address_book = recovery.Directory()
        plan = self.session.bot.plan(['Ось квитанція', recovery.ADDRESS, 'А на зиму вони підходять?'],
                                     self.session.context, '101', has_attachments=True)
        self.assertIn('зиму', plan.text)
        self.assertIn('Усе правильно?', plan.text)
        self.assertEqual(plan.context['shipping_details']['warehouse_ref'], 'branch-1')
        self.assertTrue(plan.context['receipt_submitted'])
        self.assertFalse(plan.context.get('payment_verified'))

    def test_waiting_for_a_receipt_is_not_missing_contact_information(self):
        self.order()
        plan = self.session.turn('Дякую, зараз скину квитанцію і данні')
        self.assertIn('на зв’язку', plan.text)
        self.assertFalse(self.session.context.get('shipping_details'))
        self.assertFalse(self.session.context.get('payment_reported'))

    def test_transfer_report_and_greetings_do_not_restart_shipping_questions(self):
        self.order()
        plan = self.session.bot.plan(['ок, скинув, тд тд', 'Добрий день', 'Добрий день'], self.session.context, '101')
        self.assertTrue(plan.on_send.get('payment_reported'))
        self.assertFalse(plan.context.get('payment_verified'))
        self.assertNotIn('Для відправки ще потрібні', plan.text)

    def test_repeat_of_already_recorded_quantity_keeps_instructions_short(self):
        self.order()
        self.session.turn('Я хочу дві замовити')
        invoice = self.session.context['invoice_id']
        plan = self.session.turn('Так я хочу дві')
        self.assertIn('вже записано 2 шт.', plan.text)
        self.assertEqual(self.session.context['invoice_id'], invoice)
        self.assertNotIn('Для відправки ще потрібні', plan.text)

    def test_no_invented_dispatch_date_in_transit_question(self):
        self.order()
        plan = self.session.turn('сколько примерно ехать будут?')
        self.assertIn('перевізника', plan.text)
        self.assertNotIn('сьогодні', plan.text)

    def test_short_ukrposhta_question_does_not_confirm_olx_purchase(self):
        self.session.order()
        plan = self.session.turn('На укр можна?')
        self.assertIn('Укрпоштою', plan.text)
        self.assertIn('лише через OLX', plan.text)
        self.assertFalse(self.session.context.get('checkout_method'))
        self.assertFalse(self.session.context.get('olx_delivery_reported'))

    def test_generic_help_does_not_erase_active_order(self):
        self.order()
        self.session.turn(recovery.ADDRESS)
        self.session.turn('так')
        before = self.session.context['invoice_id']
        plan = self.session.turn('поможи мені з вибором')
        self.assertEqual(self.session.context['invoice_id'], before)
        self.assertEqual(self.session.context['shipping_details']['branch'], 'Відділення №1')
        self.assertFalse(plan.guard.get('archive_order'))

    def test_short_followup_explains_the_size_already_recommended(self):
        self.session.catalog.products['suvene-pants'].update(sizing_basis='height', height_guidance={'S': [160, 173]})
        self.session.turn('Добрий день, чорні ще є?')
        self.session.turn('Вони підійдуть на ріст 165 вага 65?')
        for followup in ('Дуже?', 'Прям дуже?'):
            plan = self.session.turn(followup)
            self.assertIn('заміри', plan.text)
            self.assertEqual(self.session.context['size'], 'S')
            self.assertFalse(self.session.context.get('invoice_issued'))

    def test_dispatch_request_requires_a_bound_order(self):
        plan = self.session.turn(NOTICE)
        self.assertNotIn('напишу сюди', plan.text)
        self.assertFalse(self.session.context.get('shipment_notice_invoice'))
        self.order()
        plan = self.session.turn('Напишіть після відправлення замовлення RQ-000000000000')
        self.assertNotIn('напишу сюди', plan.text)
        self.assertFalse(self.session.context.get('shipment_notice_invoice'))


class ShipmentNoticeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = carrier.WorkerDelivery()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.worker = self.f.worker
        self.queue = self.fixture.service.queue
        self.f.process('2')
        self.f.process(fulfillment.ADDRESS)
        self.f.process('так, усе правильно')

    def create_waybill(self):
        self.assertTrue(self.fixture.service.process_one())
        self.worker.parcel_updates()
        return self.queue.updates('a')[0]

    def mark_shipped(self, row):
        with self.queue.db() as db:
            db.execute("UPDATE parcels SET ledgerly_status='shipped',updated=? WHERE id=?", (time.time(), row['id']))
        self.worker.parcel_updates()

    def drain(self):
        for _ in range(8):
            self.worker.send_customer_update()

    def restart_updates(self):
        self.worker.customer_updates = CustomerUpdates(self.f.store)
        self.worker.customer_updates.recover()
        self.worker.fulfillment_cursor = 0
        self.worker.parcel_updates()

    def notices(self):
        return [(thread, body) for thread, body in self.f.client.sent if SHIPMENT_NOTICE in body]

    def test_notice_after_dispatch_once_even_if_buyer_repeats_and_worker_restarts(self):
        self.f.process(NOTICE)
        self.assertIn('після відправлення', self.f.client.sent[-1][1])
        invoice = self.f.context()['invoice_id']
        self.assertEqual(self.f.context()['shipment_notice_invoice'], invoice)
        self.f.process('повідомте після відправлення')
        row = self.create_waybill()
        self.drain()
        self.assertEqual(self.notices(), [])
        self.assertIn('Ось ваша ТТН', self.f.client.sent[-1][1])
        self.mark_shipped(row)
        self.restart_updates()
        self.drain()
        self.assertEqual([thread for thread, _ in self.notices()], ['t1'])
        self.assertIn(fulfillment.TTN, self.notices()[0][1])
        self.restart_updates()
        self.drain()
        self.assertEqual(len(self.notices()), 1)
        self.assertFalse(self.f.context('t2').get('shipment_notice_invoice'))

    def test_manual_mode_captures_the_request_without_interrupting_the_owner(self):
        self.worker.handoffs.mode('a', 't1', 'human')
        count = len(self.f.client.sent)
        self.f.process(NOTICE)
        self.assertEqual(len(self.f.client.sent), count)
        self.assertEqual(self.f.context()['shipment_notice_invoice'], self.f.context()['invoice_id'])
        row = self.create_waybill()
        self.mark_shipped(row)
        self.drain()
        self.assertEqual([thread for thread, _ in self.notices()], ['t1'])

    def test_cancelled_parcel_does_not_send_dispatch_notice(self):
        self.f.process(NOTICE)
        row = self.create_waybill()
        self.mark_shipped(row)
        # Simulate a carrier correction before the worker sends a pending notice.
        with self.queue.db() as db:
            db.execute("UPDATE parcels SET nova_status='cancelled',ledgerly_status='cancelled',updated=? WHERE id=?",
                       (time.time(), row['id']))
        self.drain()
        self.assertEqual(self.notices(), [])

    def test_existing_owner_reply_for_the_exact_waybill_suppresses_duplicate(self):
        self.f.process(NOTICE)
        row = self.create_waybill()
        self.f.client.receive('Вже відправив, ТТН ' + fulfillment.TTN, 't1', 'sent')
        self.mark_shipped(row)
        self.drain()
        self.assertEqual(self.notices(), [])
        with self.f.store.db() as db:
            notice = db.execute("SELECT status,note FROM customer_updates WHERE kind='shipped'").fetchone()
        self.assertEqual(tuple(notice), ('sent', 'already_in_olx'))

    def test_new_purchase_does_not_rebind_an_older_shipment_request(self):
        self.f.process(NOTICE)
        row = self.create_waybill()
        self.f.process('нове замовлення')
        self.assertNotEqual(self.f.context().get('invoice_id'), row['invoice_id'])
        self.assertFalse(self.f.context().get('shipment_notice_invoice'))
        self.mark_shipped(row)
        self.drain()
        self.assertEqual([thread for thread, _ in self.notices()], ['t1'])
        self.assertIn(fulfillment.TTN, self.notices()[0][1])


if __name__ == '__main__':
    unittest.main()
