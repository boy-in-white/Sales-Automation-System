"""Carrier/IPC boundaries, no real labels, customer messages or bank operations."""

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_consultation_flow as consultation
import test_sales_journey as journey
from fulfillment import Fulfillment, active_profile, parcel_payload
from fulfillment_queue import ParcelQueue
from nova_shipping import NovaShipping, CarrierError
from telegram_bridge import Bridge

ADDRESS = 'Коваленко Олег Петрович\n0671234567\nЛьвів, Львівська область\n12'
TTN = '20450000000001'


def settings(path):
    profile = {'nova_api_key': 'test', 'cod_mode': 'money_transfer',
               'sender': {'city_ref': 'city', 'warehouse_ref': 'sender-branch', 'counterparty_ref': 'sender', 'contact_ref': 'sender-contact', 'phone': '380681234567'},
               'parcel': {'weight_kg': 2, 'length_cm': 30, 'width_cm': 20, 'height_cm': 10, 'max_automatic_items': 4}}
    return {'enabled': True, 'bridge_db': str(path / 'bridge.sqlite3'), 'schedule': {'first_half': 'first', 'second_half': 'second'},
            'profiles': {'first': profile, 'second': dict(profile, nova_api_key='test-second')}}


class Api:
    def __init__(self):
        self.calls, self.fail_save, self.city_ambiguous, self.on_save = [], False, False, None

    def request(self, model, method, props, write):
        self.calls.append((model, method, props, write))
        if method == 'getCities':
            data = [{'Ref': 'city', 'Description': 'Львів', 'AreaDescription': 'Львівська область'}]
            if self.city_ambiguous:
                data += [dict(data[0], Ref='different-city')]
        elif method == 'getWarehouses':
            data = [{'Ref': 'branch', 'CityRef': 'city', 'Number': '12', 'CategoryOfWarehouse': 'Branch', 'WarehouseStatus': 'Working'}]
        elif model == 'Counterparty':
            data = [{'Ref': 'recipient', 'ContactPerson': {'data': [{'Ref': 'recipient-contact', 'Phones': '380671234567'}]}}]
        else:
            if self.on_save:
                self.on_save()
            if self.fail_save:
                raise CarrierError('carrier_result_unknown', True)
            data = [{'IntDocNumber': TTN, 'Ref': 'document-ref'}]
        return {'success': True, 'data': data}

    def saves(self):
        return [props for model, method, props, write in self.calls if model == 'InternetDocument' and method == 'save']


class ParcelTests(unittest.TestCase):
    def setUp(self):
        calendar = patch('fulfillment.active_profile', return_value='first')
        calendar.start()
        self.addCleanup(calendar.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = settings(self.root)
        self.save_config()
        self.s = consultation.ConsultationTests()
        self.s.setUp()
        self.api = Api()
        self.service = Fulfillment(self.root, threading.Event(), self.s.catalog,
                                   carrier_factory=lambda profile: NovaShipping(profile, self.api.request), inventory=self.s.stock)

    def save_config(self):
        (self.root / 'shipping-settings.json').write_text(json.dumps(self.config))

    def order(self, method='2'):
        self.s.order()
        self.s.turn(method)
        self.s.context['fulfillment_opt_in'] = True
        self.s.turn(ADDRESS)
        self.s.turn('так')
        return parcel_payload(self.s.catalog, self.s.context, 'account', 'thread', 'buyer', '101')

    def test_cod_is_created_before_payment_check_and_collects_only_the_remainder(self):
        payload = self.order()
        self.assertFalse(self.s.context.get('payment_verified'))
        row = self.service.queue.put(payload)
        self.assertTrue(self.service.process_one())
        created = self.service.queue.get(row['id'])
        self.assertEqual(created['ttn'], TTN)
        self.assertEqual(created['nova_profile'], 'first')
        props = self.api.saves()[0]
        self.assertEqual(props['BackwardDeliveryData'][0]['RedeliveryString'], '700')
        self.assertEqual(props['Cost'], '800')
        self.assertEqual((props['CargoType'], props['SeatsAmount'], props['PayerType']), ('Parcel', '1', 'Recipient'))

    def test_full_payment_never_adds_cash_collection(self):
        self.service.queue.put(self.order('3'))
        self.service.process_one()
        props = self.api.saves()[0]
        self.assertNotIn('BackwardDeliveryData', props)
        self.assertNotIn('AfterpaymentOnGoodsCost', props)
        self.assertEqual(props['Cost'], '800')

    def test_cancellation_after_receipt_holds_the_existing_label_without_calling_owner(self):
        row = self.service.queue.put(self.order())
        self.assertTrue(self.service.process_one())
        self.s.context['parcel'] = {'ttn': TTN}
        self.s.turn('Квитанція', attachment=True)
        plan = self.s.turn('я передумав купувати')
        self.assertFalse(plan.on_send.get('handoff_now'))
        self.assertTrue(self.s.context.get('receipt_submitted'))
        self.assertTrue(self.s.context.get('cancellation_requested'))
        self.assertIsNone(self.service.sync('account', 'thread', 'buyer', '101', self.s.context, self.s.catalog))
        held = self.service.queue.get(row['id'])
        self.assertEqual(held['nova_status'], 'needs_review')
        self.assertEqual(held['ttn'], TTN)
        self.assertEqual(held['error'], 'order_requires_confirmation')
        self.s.turn('так')
        self.assertIsNone(parcel_payload(self.s.catalog, self.s.context, 'account', 'thread', 'buyer', '101'))
        self.assertEqual(len(self.api.saves()), 1)

    def test_business_control_of_payment_is_mutually_exclusive_with_money_transfer(self):
        self.config['profiles']['first']['cod_mode'] = 'afterpayment'
        self.save_config()
        self.service.queue.put(self.order())
        self.service.process_one()
        self.assertEqual(self.api.saves()[0]['AfterpaymentOnGoodsCost'], '700')
        self.assertNotIn('BackwardDeliveryData', self.api.saves()[0])

    def test_same_order_cannot_create_a_second_waybill(self):
        payload = self.order()
        first = self.service.queue.put(payload)
        self.service.process_one()
        self.assertEqual(first['id'], self.service.queue.put(payload)['id'])
        self.assertFalse(self.service.process_one())
        self.assertEqual(len(self.api.saves()), 1)

    def test_unknown_result_is_never_retried_after_restart(self):
        row = self.service.queue.put(self.order())
        self.api.fail_save = True
        self.service.process_one()
        self.assertEqual(self.service.queue.get(row['id'])['nova_status'], 'uncertain')
        self.service.queue.recover_carrier()
        self.assertFalse(self.service.process_one())
        self.assertEqual(len(self.api.saves()), 1)

    def test_crash_during_creating_is_recovered_as_unknown_not_pending(self):
        row = self.service.queue.put(self.order())
        self.service.queue.claim_nova()
        self.service.queue.before_write(row['id'], row['content_key'], 'creating')
        self.service.queue.recover_carrier()
        self.assertEqual(self.service.queue.get(row['id'])['nova_status'], 'uncertain')
        self.assertIsNone(self.service.queue.claim_nova())

    def test_ambiguous_city_does_not_create_a_recipient_or_a_waybill(self):
        row = self.service.queue.put(self.order())
        self.api.city_ambiguous = True
        self.service.process_one()
        self.assertEqual(self.service.queue.get(row['id'])['error'], 'recipient_city_ambiguous')
        self.assertFalse(any(write for _, _, _, write in self.api.calls))

    def test_stock_change_blocks_label_creation(self):
        row = self.service.queue.put(self.order())
        self.s.stock.values[('suvene-pants', 'M', 'BLACK')] = 0
        self.service.process_one()
        self.assertEqual(self.service.queue.get(row['id'])['error'], 'stock_changed_before_waybill')
        self.assertEqual(self.api.saves(), [])

    def test_missing_second_profile_never_falls_back_to_first(self):
        self.config['profiles']['second']['nova_api_key'] = ''
        self.save_config()
        row = self.service.queue.put(self.order())
        with patch('fulfillment.active_profile', return_value='second'):
            self.service.process_one()
        self.assertIn('active_profile_not_configured', self.service.queue.get(row['id'])['error'])
        self.assertEqual(self.api.calls, [])

    def test_second_half_uses_second_profile_and_keeps_older_labels_bound_to_first(self):
        row = self.service.queue.put(self.order())
        self.service.process_one()
        with patch('fulfillment.active_profile', return_value='second'):
            self.assertFalse(self.service.process_one())
        self.assertEqual(self.service.queue.get(row['id'])['nova_profile'], 'first')

    def test_switch_during_preparation_restarts_before_document_creation(self):
        row = self.service.queue.put(self.order())
        with patch('fulfillment.active_profile', side_effect=['first', 'second']):
            self.service.process_one()
        self.assertEqual(self.api.saves(), [])
        self.assertEqual(self.service.queue.get(row['id'])['nova_status'], 'pending')
        with patch('fulfillment.active_profile', return_value='second'):
            self.service.process_one()
        self.assertEqual(self.service.queue.get(row['id'])['nova_profile'], 'second')
        self.assertEqual(len(self.api.saves()), 1)

    def test_order_change_while_save_is_in_flight_keeps_the_returned_ttn_for_review(self):
        payload = self.order()
        row = self.service.queue.put(payload)
        changed = copy.deepcopy(payload)
        changed['recipient']['branch'] = 'Відділення №15'
        self.api.on_save = lambda: self.service.queue.put(changed)
        self.service.process_one()
        saved = self.service.queue.get(row['id'])
        self.assertEqual(saved['ttn'], TTN)
        self.assertEqual(saved['nova_status'], 'needs_review')
        self.assertIsNone(self.service.queue.claim_ledgerly())
        self.assertFalse(self.service.process_one())

    def test_other_account_or_thread_has_a_separate_queue_identity(self):
        payload = self.order()
        first = self.service.queue.put(payload)
        second = self.service.queue.put(dict(payload, account='other'))
        third = self.service.queue.put(dict(payload, thread='other'))
        self.assertEqual(len({first['id'], second['id'], third['id']}), 3)

    def test_old_orders_and_olx_delivery_never_enter_the_auto_creation_queue(self):
        self.order()
        self.s.context.pop('fulfillment_opt_in')
        self.assertIsNone(parcel_payload(self.s.catalog, self.s.context, 'a', 't', 'b', '101'))
        self.s.context.update(fulfillment_opt_in=True, checkout_method='olx')
        self.assertIsNone(parcel_payload(self.s.catalog, self.s.context, 'a', 't', 'b', '101'))

    def test_cancelled_order_does_not_create_a_label_or_enter_ledgerly(self):
        row = self.service.queue.put(self.order())
        self.service.queue.cancel('account', 'thread', row['invoice_id'])
        self.assertFalse(self.service.process_one())
        self.assertEqual(self.api.saves(), [])
        self.assertIsNone(self.service.queue.claim_ledgerly())

    def test_cancel_while_creation_is_in_flight_preserves_the_returned_label_without_publishing(self):
        row = self.service.queue.put(self.order())
        self.api.on_save = lambda: self.service.queue.cancel('account', 'thread', row['invoice_id'])
        self.service.process_one()
        current = self.service.queue.get(row['id'])
        self.assertEqual(current['nova_status'], 'cancelled')
        self.assertEqual(current['ttn'], TTN)
        self.assertIsNone(self.service.queue.claim_ledgerly())

    def test_paused_or_closed_chat_is_not_sent_to_the_carrier(self):
        row = self.service.queue.put(self.order())
        self.service.can_create = lambda item: False
        self.service.process_one()
        self.assertEqual(self.api.saves(), [])
        self.assertFalse(any(write for _, _, _, write in self.api.calls))
        self.assertEqual(self.service.queue.get(row['id'])['error'], 'order_no_longer_active')

    def test_configuration_error_can_resume_after_the_active_key_is_filled(self):
        self.config['profiles']['second']['nova_api_key'] = ''
        self.save_config()
        row = self.service.queue.put(self.order())
        with patch('fulfillment.active_profile', return_value='second'):
            self.service.process_one()
            self.config['profiles']['second']['nova_api_key'] = 'test-second'
            self.save_config()
            self.service.process_one()
        self.assertEqual(self.service.queue.get(row['id'])['nova_profile'], 'second')
        self.assertEqual(len(self.api.saves()), 1)

    def test_customer_confirms_shipping_data_without_confirming_payment(self):
        self.s.order()
        self.s.turn('2')
        self.s.context['fulfillment_opt_in'] = True
        self.s.turn(ADDRESS)
        self.assertIsNone(parcel_payload(self.s.catalog, self.s.context, 'a', 't', 'b', '101'))
        self.s.turn('так')
        self.assertIsNotNone(parcel_payload(self.s.catalog, self.s.context, 'a', 't', 'b', '101'))

    def test_ledgerly_publish_in_flight_is_not_republished_after_restart(self):
        self.service.queue.put(self.order())
        self.service.process_one()
        row = self.service.queue.claim_ledgerly()
        self.assertTrue(self.service.queue.before_publish(row['id']))
        self.service.queue.recover_ledgerly()
        self.assertIsNone(self.service.queue.claim_ledgerly())
        self.assertEqual(self.service.queue.get(row['id'])['ledgerly_status'], 'uncertain')


class ProfileCalendarTests(unittest.TestCase):
    def test_kyiv_calendar_boundaries_including_short_and_leap_months(self):
        schedule = {'schedule': {'first_half': 'first', 'second_half': 'second'}}
        for date, expected in ((datetime(2026, 11, 15, 21, 59, tzinfo=timezone.utc), 'first'),
                               (datetime(2026, 11, 15, 22, 0, tzinfo=timezone.utc), 'second'),
                               (datetime(2026, 2, 28, 12, tzinfo=timezone.utc), 'second'),
                               (datetime(2028, 2, 29, 12, tzinfo=timezone.utc), 'second'),
                               (datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc), 'first')):
            self.assertEqual(active_profile(schedule, date), expected)


class PaymentButtonTests(unittest.TestCase):
    def setUp(self):
        calendar = patch('fulfillment.active_profile', return_value='first')
        calendar.start()
        self.addCleanup(calendar.stop)
        self.s = journey.JourneyTests()
        self.s.setUp()
        self.addCleanup(self.s.doCleanups)

    def test_owner_can_check_payment_in_one_click_before_receipt_or_manual_mode(self):
        s=self.s
        s.turn('Хочу замовити чорні M');s.owner_topic();s.turn('2')
        card=s.worker.orders.get('account','t')
        keys=Bridge.controls('bot',card)['inline_keyboard']
        self.assertEqual(sum(len(row) for row in keys),2)
        self.assertFalse(any(button['callback_data'].startswith('pay:') for row in keys for button in row))
        s.bridge.update(s.callback('pay:%s:%s:paid' % (card['order_id'],card['invoice_key'][:16]),80))
        self.assertFalse(s.worker.orders.get('account','t')['verified'])
        s.turn(ADDRESS)
        answer=s.turn('так')
        self.assertRegex(answer,'Відправка|Постараємося відправити')
        self.assertFalse(s.store.context('account','t','buyer','101',30).get('payment_verified'))

    def test_worker_queues_only_confirmed_new_order_and_refreshes_the_owner_card(self):
        s = self.s
        config = settings(s.root)
        (s.root / 'shipping-settings.json').write_text(json.dumps(config))
        api = Api()
        service = Fulfillment(s.root, s.worker.stop, s.worker.catalog,
                              carrier_factory=lambda profile: NovaShipping(profile, api.request),
                              inventory=s.stock, can_create=s.worker.parcel_can_create)
        s.worker.fulfillment = service
        s.turn('Хочу замовити чорні M')
        s.owner_topic()
        s.turn('2')
        self.assertIsNone(service.queue.claim_nova())
        s.turn(ADDRESS)
        self.assertIsNone(service.queue.claim_nova())
        s.turn('так')
        service.process_one()
        s.worker.parcel_updates()
        context = s.store.context('account', 't', 'buyer', '101', 30)
        self.assertEqual(context['parcel']['ttn'], TTN)
        row = service.queue.get(context['parcel']['id'])
        self.assertFalse(row['paid'])
        self.assertTrue(service.queue.payment(row['id'], row['invoice_key'], 7, True))
        s.worker.parcel_updates()
        self.assertTrue(s.worker.orders.get('account', 't')['verified'])
        s.bridge.update(s.callback('done', 81))
        s.worker.send_human_reply()
        self.assertEqual(service.queue.get(row['id'])['nova_status'], 'created')
        self.assertEqual(s.store.context('account', 't', 'buyer', '101', 30), {})


if __name__ == '__main__':
    unittest.main()
