"""Buyer confirmation, frozen invoices and owner-only payment bookkeeping."""

import copy
import json
import unittest

import test_consultation_flow as consultation
import test_sales_journey as journey
from order_summary import snapshot, confirmation_key

ADDRESS = "Петренко Іван Іванович\n0671234567\nЛьвів, Львівська область\n12"


class BuyerOrderTests(unittest.TestCase):
    def setUp(self):
        self.session = consultation.ConsultationTests()
        self.session.setUp()
        self.session.order()
        self.invoice = self.session.turn("2")

    def test_invoice_contains_the_goods_and_agreed_total_before_card(self):
        text = self.invoice.text
        self.assertIn("Штани Suvene", text)
        self.assertIn("Разом за товари: 800", text)
        self.assertLess(text.index("Штани Suvene"), text.index("4111 1111"))
        self.assertTrue(self.session.context["invoice_issued"])

    def test_invoice_total_does_not_change_when_catalog_price_changes_later(self):
        self.session.catalog.products["suvene-pants"]["price_uah"] = 999
        plan = self.session.turn(ADDRESS)
        self.assertIn("Разом за товари: 800", plan.text)
        self.assertNotIn("999", plan.text)

    def test_receipt_is_not_customer_confirmation(self):
        self.session.turn(ADDRESS)
        receipt = self.session.turn("Квитанція", attachment=True)
        self.assertNotIn("handoff_now", receipt.on_send)
        self.assertIn("Усе правильно?", receipt.text)
        final = self.session.turn("Все правильно")
        self.assertFalse(final.on_send.get("handoff_now"))
        self.assertTrue(self.session.context["customer_confirmation"])

    def test_branch_correction_keeps_name_phone_and_city_and_requires_new_confirmation(self):
        self.session.turn(ADDRESS)
        self.session.turn("так")
        before = copy.deepcopy(self.session.context["shipping_details"])
        changed = self.session.turn("Заміни відділення на 15")
        self.assertNotIn("handoff_now", changed.on_send)
        self.assertIn("№15", changed.text)
        self.assertIn("Усе правильно?", changed.text)
        for key in ("name", "phone", "place"):
            self.assertEqual(self.session.context["shipping_details"][key], before[key])
        self.assertIsNone(self.session.context.get("customer_confirmation"))

    def test_new_phone_without_colon_is_understood(self):
        self.session.turn(ADDRESS)
        self.session.turn("так")
        changed = self.session.turn("Новий телефон 0681234567")
        self.assertIn("+380681234567", changed.text)
        self.assertEqual(self.session.context["shipping_details"]["branch"], "Відділення №12")

    def test_same_delivery_method_does_not_send_another_invoice(self):
        original_id = self.session.context["invoice_id"]
        plan = self.session.turn("Післяплата")
        self.assertNotIn("4111", plan.text)
        self.assertEqual(self.session.context["invoice_id"], original_id)

    def test_measurements_question_during_checkout_keeps_invoice_and_shipping_state(self):
        original_id = self.session.context["invoice_id"]
        plan = self.session.turn("Які заміри L?")
        self.assertIn("108", plan.text)
        self.assertIn("76", plan.text)
        self.assertNotIn("4111", plan.text)
        self.assertEqual(self.session.context["size"], "M")
        self.assertEqual(self.session.context["invoice_id"], original_id)
        self.assertEqual(self.session.context["checkout_stage"], "details")

    def test_price_question_during_checkout_uses_agreed_price_without_another_invoice(self):
        self.session.catalog.products["suvene-pants"]["price_uah"] = 999
        plan = self.session.turn("Скільки коштує замовлення?")
        self.assertIn("800 грн", plan.text)
        self.assertIn("100 грн", plan.text)
        self.assertNotIn("999", plan.text)
        self.assertNotIn("4111", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_explicit_request_repeats_only_existing_payment_details(self):
        original_id = self.session.context["invoice_id"]
        repeated = self.session.turn("Скинь реквізити ще раз")
        self.assertIn("4111 1111 1111 1111", repeated.text)
        self.assertIn("ще раз платити не потрібно", repeated.text)
        self.assertEqual(self.session.context["invoice_id"], original_id)

    def test_repeat_after_bank_account_change_keeps_payment_on_hold(self):
        self.session.bot.merchant["account"] = "4000000000000002"
        repeated = self.session.turn("Повтори реквізити")
        self.assertFalse(repeated.on_send.get('handoff_now'))
        self.assertNotIn("4000", repeated.text)
        self.assertNotIn("4111", repeated.text)

    def test_bare_branch_two_is_not_a_repeat_of_payment_method_two(self):
        self.session.turn("ПІБ: Петренко Іван Іванович\nТелефон: 0671234567\nМісто: Львів, Львівська область")
        plan = self.session.turn("2")
        self.assertIn("№2", plan.text)
        self.assertEqual(self.session.context["shipping_details"]["branch"], "Відділення №2")

    def test_confirmation_cannot_overwrite_city_with_the_word_yes(self):
        self.session.turn("ПІБ: Петренко Іван Іванович\nТелефон: 0671234567\nВідділення: 12")
        self.session.turn("Львів, Львівська область")
        self.session.turn("так")
        self.assertEqual(self.session.context["shipping_details"]["place"], "Львів, Львівська область")
        order = snapshot(self.session.catalog, self.session.context)
        self.assertEqual(self.session.context["customer_confirmation"], confirmation_key(order))

    def test_no_answer_to_confirmation_does_not_change_shipping_data(self):
        self.session.turn(ADDRESS)
        old = copy.deepcopy(self.session.context["shipping_details"])
        plan = self.session.turn("Ні")
        self.assertIn("виправити", plan.text)
        self.assertEqual(self.session.context["shipping_details"], old)
        self.assertNotIn("handoff_now", plan.on_send)
        self.assertFalse(self.session.turn("Ні").on_send.get('handoff_now'))

    def test_confirmation_does_not_repeat_forever_if_customer_cannot_clarify(self):
        self.session.turn(ADDRESS)
        first = self.session.turn("не розумію")
        self.assertNotIn("handoff_now", first.on_send)
        self.assertNotIn("Петренко", first.text)
        second = self.session.turn("все одно не розумію")
        self.assertFalse(second.on_send.get('handoff_now'))

    def test_cancel_after_receipt_keeps_payment_information(self):
        self.session.turn("Квитанція", attachment=True)
        plan = self.session.turn("Не треба")
        self.assertFalse(plan.on_send.get('handoff_now'))
        self.assertTrue(self.session.context["receipt_submitted"])

    def test_prepaid_amount_cannot_exceed_the_goods_total(self):
        other = consultation.ConsultationTests()
        other.setUp()
        other.catalog.products["suvene-pants"]["price_uah"] = 80
        other.order()
        plan = other.turn("2")
        self.assertFalse(plan.on_send.get('handoff_now'))
        self.assertNotIn("4111", plan.text)

    def test_already_issued_legacy_invoice_resumes_without_asking_for_another_payment(self):
        context = self.session.context
        for key in ("invoice_id", "invoice_items", "invoice_total", "invoice_issued", "invoice_payment_signature"):
            context.pop(key, None)
        plan = self.session.turn(ADDRESS)
        self.assertIn("Усе правильно?", plan.text)
        self.assertNotIn("4111", plan.text)
        self.assertTrue(self.session.context["invoice_issued"])
        self.assertTrue(self.session.context["invoice_id"])
        self.assertRegex(self.session.turn("так").text, "Відправка|Постараємося відправити")

    def test_payment_report_without_current_context_does_not_request_payment_again(self):
        self.session.context = {}
        plan = self.session.turn("Я оплатив учора")
        self.assertFalse(plan.on_send.get('handoff_now'))
        self.assertNotIn("4111", plan.text)
        self.assertNotIn("Оформити можна", plan.text)
        self.session.context = {}
        self.assertNotIn("handoff_now", self.session.turn("Я ще не оплатив").on_send)


class OperatorCardTests(unittest.TestCase):
    def setUp(self):
        self.scene = journey.JourneyTests()
        self.scene.setUp()
        self.addCleanup(self.scene.doCleanups)

    def complete(self):
        s = self.scene
        s.turn("Хочу замовити чорні M")
        s.owner_topic()
        s.turn("2")
        s.turn(ADDRESS)
        s.turn("так")
        s.turn("Квитанція", attachment=True)
        self.assertEqual(s.worker.handoffs.get("account", "t")["mode"], "bot")
        return s.worker.orders.get("account", "t")

    def ledgerly_decision(self, card, verified=True, event='ledgerly-paid'):
        # Exercise the trusted business-card boundary; Telegram pay:* is retired.
        return self.scene.worker.orders.verify('account','t',card['order_id'],card['invoice_key'][:16],7,verified,event)

    def payment_callback(self, card, update=40, owner=7, action="paid"):
        result = self.scene.callback("pay:%s:%s:%s" % (card["order_id"], card["invoice_key"][:16], action), update)
        result["callback_query"]["from"]["id"] = owner
        return result

    def test_panel_shows_invoice_shipping_and_actual_deposit_to_check(self):
        card = self.complete()
        s = self.scene
        text = s.worker.orders.text("account", "t", "Панель") + "\n" + s.worker.orders.details("account", "t")
        self.assertIn("Ledgerly", text)
        self.assertIn("Разом за товари: 800", text)
        self.assertIn("Передоплата: 100", text)
        self.assertIn("+380671234567", text)
        self.assertIn("Вкладення для перевірки оплати: отримане", text)
        self.assertNotIn("4111", text)

    def test_owner_payment_mark_does_not_send_any_message_to_buyer(self):
        card = self.complete()
        s = self.scene
        before = len(s.client.sent)
        self.ledgerly_decision(card)
        self.assertTrue(s.worker.orders.get("account", "t")["verified"])
        self.assertEqual(len(s.client.sent), before)
        self.assertTrue(s.store.context("account", "t", "buyer", "101", 30)["payment_verified"])
        self.assertIn("Оплату позначено", s.worker.orders.text("account", "t", "Панель"))

    def test_verified_order_can_answer_dispatch_when_owner_resumes_bot(self):
        card = self.complete()
        s = self.scene
        self.ledgerly_decision(card)
        s.bridge.update(s.callback("bot", 41))
        s.worker.send_human_reply()
        answer = s.turn("Коли відправка?")
        self.assertIn("сьогодні", answer)
        self.assertNotIn("4111", answer)
        self.assertEqual(s.worker.handoffs.get("account", "t")["mode"], "bot")

    def test_payment_mark_is_idempotent_and_can_be_undone(self):
        card = self.complete()
        s = self.scene
        update = self.payment_callback(card)
        self.ledgerly_decision(card)
        self.ledgerly_decision(card)
        with s.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM payment_decisions").fetchone()[0], 1)
        self.ledgerly_decision(card, False, "ledgerly-unpaid")
        self.assertFalse(s.worker.orders.get("account", "t")["verified"])
        self.assertFalse(s.store.context("account", "t", "buyer", "101", 30)["payment_verified"])

    def test_another_owner_id_and_stale_invoice_button_cannot_mark_payment(self):
        card = self.complete()
        s = self.scene
        s.bridge.update(self.payment_callback(card, owner=8))
        self.assertFalse(s.worker.orders.get("account", "t")["verified"])
        stale = dict(card, order_id="OLDINVOICE")
        s.bridge.update(self.payment_callback(stale, update=41))
        self.assertFalse(s.worker.orders.get("account", "t")["verified"])

    def test_old_button_cannot_verify_a_changed_amount_under_the_same_order_id(self):
        card = self.complete()
        s = self.scene
        context = s.store.context("account", "t", "buyer", "101", 30)
        context["checkout_amount"] = "200"
        s.store.save_context("account", "t", "buyer", "101", context)
        s.worker.orders.sync("account", "t", context, s.worker.catalog)
        self.ledgerly_decision(card)
        self.assertFalse(s.worker.orders.get("account", "t")["verified"])

    def test_done_clears_current_card_but_keeps_owner_decision_audit(self):
        card = self.complete()
        s = self.scene
        self.ledgerly_decision(card)
        s.bridge.update(s.callback("done", 42))
        s.worker.send_human_reply()
        self.assertIsNone(s.worker.orders.get("account", "t"))
        with s.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM payment_decisions").fetchone()[0], 1)
        s.bridge.update(self.payment_callback(card, update=43))
        self.assertIsNone(s.worker.orders.get("account", "t"))

    def test_panel_updates_are_coalesced_and_do_not_change_customer_routing(self):
        self.complete()
        s = self.scene
        with s.store.db() as db:
            count = db.execute("SELECT COUNT(*) FROM bridge_outbox WHERE kind='control' AND status='pending'").fetchone()[0]
        self.assertEqual(count, 1)
        s.bridge.next_send = 0
        s.bridge.deliver()
        updates = [fields for method, fields in s.api.calls if method == "editMessageText"]
        self.assertTrue(updates)
        self.assertIn("Ledgerly", updates[-1]["text"])
        self.assertNotIn("message_thread_id", updates[-1])
        self.assertEqual(s.worker.handoffs.get("account", "t")["mode"], "bot")

    def test_unpaid_cancellation_clears_the_old_invoice_from_the_panel(self):
        s = self.scene
        s.turn("Хочу замовити чорні M")
        s.turn("2")
        self.assertIsNotNone(s.worker.orders.get("account", "t"))
        s.turn("Не треба")
        self.assertIsNone(s.worker.orders.get("account", "t"))
        context = s.store.context("account", "t", "buyer", "101", 30)
        self.assertNotIn("checkout_amount", context)
        self.assertNotIn("checkout_method", context)

    def test_manual_card_update_after_payment_mark_preserves_verified_status(self):
        card = self.complete()
        s = self.scene
        self.ledgerly_decision(card)
        context = s.store.context("account", "t", "buyer", "101", 30)
        context["shipping_details"]["branch"] = "Відділення №15"
        s.worker.orders.sync("account", "t", context, s.worker.catalog)
        self.assertTrue(s.worker.orders.get("account", "t")["verified"])

    def test_existing_human_checkout_gets_a_card_at_startup_without_reissuing_payment(self):
        s = self.scene
        s.worker.handoffs.waiting("account", "t", "buyer", "101", 0, "Legacy customer")
        with s.store.db() as db:
            db.execute("UPDATE handoffs SET mode='human',group_id=-100123,topic_id=10 WHERE thread='t'")
        s.store.save_context("account", "t", "buyer", "101", {
            "product": "suvene-pants", "size": "M", "color": "BLACK", "checkout_stage": "owner",
            "checkout_method": "cod", "checkout_amount": "100", "receipt_submitted": True,
        })
        s.worker.telegram_config = None
        s.worker.initialize()
        card = s.worker.orders.get("account", "t")
        self.assertTrue(card["order_id"])
        self.assertTrue(card["data"]["issued"])
        self.assertEqual(card["data"]["amount"], "100")
        self.assertIsNone(card["data"]["total"])
        self.assertFalse(card["verified"])
        self.assertEqual(s.client.sent, [])
        self.assertEqual(s.worker.handoffs.get("account", "t")["mode"], "human")
        context = s.store.context("account", "t", "buyer", "101", 30)
        context["checkout_amount"] = "200"
        s.store.save_context("account", "t", "buyer", "101", context)
        s.worker.initialize()
        self.assertEqual(s.worker.orders.get("account", "t")["data"]["amount"], "200")
        with s.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bridge_outbox WHERE kind='control' AND status='pending'").fetchone()[0], 1)

    def test_old_payment_audit_and_cancelled_messages_are_purged_but_current_card_remains(self):
        card = self.complete()
        s = self.scene
        self.ledgerly_decision(card)
        with s.store.db() as db:
            db.execute("UPDATE payment_decisions SET created=0")
            s.worker.handoffs.enqueue(db, "old-cancelled", "account", "t", "notice", "Old private message")
            db.execute("UPDATE bridge_outbox SET status='cancelled',created=0 WHERE event_key='old-cancelled'")
        s.worker.orders.cleanup(30)
        s.worker.handoffs.cleanup(30)
        self.assertIsNotNone(s.worker.orders.get("account", "t"))
        with s.store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM payment_decisions").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT 1 FROM bridge_outbox WHERE event_key='old-cancelled'").fetchone())


if __name__ == "__main__":
    unittest.main()
