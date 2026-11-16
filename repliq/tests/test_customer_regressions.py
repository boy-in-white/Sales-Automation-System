"""Replay real failure shapes, with fake customers, fake stock and no live APIs."""

import copy
import unittest

import test_consultation_flow as consultation
import test_sales_journey as journey
from customer_language import intents, requested_quantity
from checkout import shipping_details
from order_summary import snapshot, invoice_key

NAME = "Коваленко Олег Петрович"
REST = "0671234567\nЛьвів, Львівська область\n12"
ADDRESS = NAME + "\n" + REST


class CustomerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.s = consultation.ConsultationTests()
        self.s.setUp()

    def checkout(self, method="2"):
        self.s.order()
        return self.s.turn(method)

    def test_body_parameters_survive_dont_know_and_customer_can_choose_cyrillic_l(self):
        s = self.s
        for text in ("Привіт", "Так, який розмір мені підійде", "не знаю, але ріст в мене 180, і вага 78"):
            plan = s.turn(text, "102")
            self.assertNotIn("handoff_now", plan.on_send)
            self.assertNotIn("S: довжина", plan.text)
        self.assertEqual((s.context["height"], s.context["weight"]), (180, 78))
        self.assertIn("Вам підійде L", plan.text)
        plan = s.turn("добре, давай розмір Л", "102")
        self.assertEqual(s.context["size"], "L")
        self.assertNotIn("параметр", plan.text)
        self.assertIn("колір", plan.text)
        s.turn("давай білий", "102")
        before = copy.deepcopy(s.context)
        plan = s.turn("кидай рахунок", "102")
        self.assertIn("100 грн", plan.text)
        self.assertIn("повної оплати", plan.text)
        self.assertNotIn("що саме хочете уточнити", plan.text)
        self.assertNotIn("4111", plan.text)
        for key in ("product", "size", "color"):
            self.assertEqual(s.context[key], before[key])
        plan = s.turn("наложкою", "102")
        self.assertIn("4111 1111", plan.text)
        self.assertEqual(s.context["invoice_items"][0]["size"], "L")

    def test_older_waiting_for_garment_context_does_not_drop_height_or_handoff(self):
        self.s.context = {"product": "gallery-tee", "awaiting": "garment_measurements", "greeted": True}
        plan = self.s.turn("не знаю, ріст в мене 180, вага 78", "102")
        self.assertEqual(self.s.context["height"], 180)
        self.assertEqual(self.s.context["weight"], 78)
        self.assertEqual(self.s.context["size"], "L")
        self.assertEqual(self.s.context["awaiting"], "color")
        self.assertNotIn("handoff_now", plan.on_send)

    def test_invoice_colloquialisms_do_not_guess_payment_amount(self):
        for text in ("кидай рахунок", "скинь реквізити", "куда платить?", "пришли счет"):
            self.s.context = {}
            self.s.order()
            plan = self.s.turn(text)
            self.assertIn("100 грн", plan.text)
            self.assertNotIn("4111", plan.text)
            self.assertNotIn("handoff_now", plan.on_send)
            self.assertNotIn("invoice_issued", self.s.context)

    def test_repeated_size_advice_keeps_the_selected_l_instead_of_dumping_all_sizes(self):
        self.s.turn("давай розмір Л", "102")
        plan = self.s.turn("ну так який розмір мені підійде?", "102")
        self.assertIn("Ви обрали L", plan.text)
        self.assertIn("зріст і вагу", plan.text)
        self.assertNotIn("S:", plan.text)
        self.assertEqual(self.s.context["size"], "L")
        self.assertNotIn("handoff_now", plan.on_send)

    def test_unlabelled_photo_is_not_assumed_to_be_a_receipt(self):
        self.checkout()
        plan = self.s.turn("", attachment=True)
        self.assertFalse(self.s.context.get("receipt_submitted"))
        self.assertTrue(self.s.context["unclassified_attachment"])
        self.assertIn("Це квитанція", plan.text)
        self.assertNotIn("Оплату ще", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)
        self.s.turn("це не квитанція, випадково надіслав")
        self.assertFalse(self.s.context.get("receipt_submitted"))
        self.assertFalse(self.s.context.get("unclassified_attachment"))

    def test_unlabelled_photo_can_be_identified_without_uploading_it_again(self):
        self.checkout()
        self.s.turn("", attachment=True)
        plan = self.s.turn("так, це квитанція")
        self.assertTrue(self.s.context["receipt_submitted"])
        self.assertFalse(self.s.context.get("unclassified_attachment"))
        self.assertIn("ПІБ:", plan.text)
        self.assertIn("Номер телефону:", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_future_receipt_promise_does_not_identify_an_unrelated_photo(self):
        self.checkout()
        self.s.turn("", attachment=True)
        self.s.turn("квитанцію надішлю пізніше")
        self.assertFalse(self.s.context.get("receipt_submitted"))
        self.assertTrue(self.s.context.get("unclassified_attachment"))

    def test_caption_explicitly_saying_not_a_receipt_overrides_expected_receipt_stage(self):
        self.checkout()
        self.s.turn(ADDRESS)
        self.s.turn("так")
        self.s.turn("це не квитанція, а фото товару", attachment=True)
        self.assertFalse(self.s.context.get("receipt_submitted"))

    def test_stock_question_during_checkout_does_not_change_quantity_or_customer_name(self):
        self.checkout()
        self.s.turn(NAME)
        plan = self.s.turn("А є 2 футболки L білі?")
        self.assertIn("наявності", plan.text)
        self.assertEqual(self.s.context["invoice_items"][0]["quantity"], 1)
        self.assertEqual(self.s.context["shipping_details"], {"name": NAME})
        self.assertNotIn("handoff_now", plan.on_send)

    def test_photo_purpose_yes_is_not_also_order_confirmation(self):
        self.checkout()
        self.s.turn(ADDRESS)
        self.s.turn("", attachment=True)
        plan = self.s.turn("так")
        self.assertTrue(self.s.context["receipt_submitted"])
        self.assertFalse(self.s.context.get("customer_confirmation"))
        self.assertNotIn("handoff_now", plan.on_send)
        self.assertFalse(self.s.turn("так").on_send.get("handoff_now"))
        self.assertTrue(self.s.context["customer_confirmation"])

    def test_ignored_photo_question_does_not_discard_shipping_progress(self):
        self.checkout()
        self.s.turn("", attachment=True)
        plan = self.s.turn(NAME)
        self.assertEqual(self.s.context["shipping_details"], {"name": NAME})
        self.assertIn("Номер телефону:", plan.text)
        self.assertIn("Місто або село, область:", plan.text)
        self.assertIn("Номер відділення:", plan.text)
        self.s.turn(REST)
        plan = self.s.turn("так")
        self.assertRegex(plan.text, "Відправка|Постараємося відправити")
        self.assertFalse(self.s.context.get("receipt_submitted"))
        plan = self.s.turn("так")
        self.assertFalse(plan.on_send.get("handoff_now"))

    def test_request_for_all_fields_together_does_not_repeat_one_question_or_save_profanity(self):
        self.checkout()
        self.s.turn(NAME)
        plan = self.s.turn("бля, та давай всьо разом")
        self.assertEqual(self.s.context["shipping_details"], {"name": NAME})
        self.assertIn("Номер телефону:", plan.text)
        self.assertIn("Місто або село, область:", plan.text)
        self.assertIn("Номер відділення:", plan.text)
        self.assertNotIn("ПІБ:", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)
        plan = self.s.turn(REST)
        self.assertIn("Усе правильно?", plan.text)
        self.assertEqual(len(self.s.context["shipping_details"]), 4)

    def test_remaining_two_unlabelled_shipping_lines_are_collected_together(self):
        details = shipping_details("Львів, Львівська область\n12", {"name": NAME, "phone": "+380671234567"}, "place")
        self.assertEqual(details["place"], "Львів, Львівська область")
        self.assertEqual(details["branch"], "Відділення №12")

    def test_plain_question_is_not_saved_as_a_persons_name(self):
        self.assertEqual(shipping_details("А як можна замовити?", {}, "name"), {})

    def test_changed_mind_with_positive_order_is_not_cancellation(self):
        text = "поняв. давай я передумав, я хочу дві замовити"
        self.assertNotIn("cancel", intents(text))
        self.assertEqual(requested_quantity(text), 2)
        self.assertIn("cancel", intents("я передумав"))

    def test_cod_amendment_rechecks_stock_keeps_address_and_does_not_reissue_card(self):
        self.checkout()
        self.s.turn(NAME)
        old_id = self.s.context["invoice_id"]
        plan = self.s.turn("поняв. Давай я передумав, я хочу дві замовити")
        self.assertEqual(self.s.context["quantity"], 2)
        self.assertEqual(self.s.context["invoice_items"][0]["quantity"], 2)
        self.assertEqual(self.s.context["checkout_amount"], "100")
        self.assertEqual(self.s.context["invoice_total"], "1600")
        self.assertEqual(self.s.context["invoice_id"], old_id)
        self.assertEqual(self.s.context["shipping_details"], {"name": NAME})
        self.assertIn("1600", plan.text)
        self.assertIn("залишається 100 грн", plan.text)
        self.assertNotIn("скасовано", plan.text)
        self.assertNotIn("4111", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)
        self.assertIn("stocks", plan.guard)

    def test_cod_change_after_address_review_requires_confirmation_of_new_goods(self):
        self.checkout()
        self.s.turn(ADDRESS)
        self.s.turn("так")
        plan = self.s.turn("хочу замовити 2")
        self.assertIn("Усе правильно?", plan.text)
        self.assertFalse(self.s.context.get("customer_confirmation"))
        self.assertIn("1600", plan.text)

    def test_unavailable_quantity_preserves_original_order_and_amount(self):
        self.checkout()
        self.s.stock.values[("suvene-pants", "M", "BLACK")] = 1
        old = copy.deepcopy(self.s.context["invoice_items"])
        plan = self.s.turn("хочу дві")
        self.assertIn("доступно 1 шт", plan.text)
        self.assertEqual(self.s.context["invoice_items"], old)
        self.assertEqual(self.s.context["invoice_total"], "800")
        self.assertNotIn("handoff_now", plan.on_send)

    def test_full_payment_change_updates_total_without_asking_about_payment(self):
        self.checkout("3")
        old = copy.deepcopy(self.s.context)
        plan = self.s.turn("хочу дві")
        self.assertEqual(self.s.context["checkout_amount"], "1600")
        self.assertEqual(self.s.context["invoice_id"], old["invoice_id"])
        self.assertEqual(self.s.context["previous_invoice_amounts"], ["800"])
        self.assertIn("Оновлена загальна вартість", plan.text)
        self.assertNotIn("переказували", plan.text)
        self.assertNotIn("вже оплатили", plan.text)
        self.assertNotIn("4111", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_full_payment_already_paid_change_preserves_invoice_without_new_charge(self):
        self.checkout("3")
        self.s.turn("Оплатив")
        plan = self.s.turn("хочу дві")
        self.assertFalse(plan.on_send.get('handoff_now'))
        self.assertEqual(self.s.context["checkout_amount"], "800")
        self.assertEqual(self.s.context["invoice_items"][0]["quantity"], 1)
        self.assertIn("оплачувати не потрібно", plan.text)

    def test_full_payment_amendment_rechecks_stock_without_payment_question(self):
        self.checkout("3")
        self.s.stock.values[("suvene-pants", "M", "BLACK")] = 1
        plan = self.s.turn("хочу дві")
        self.assertIn("доступно 1 шт", plan.text)
        self.assertEqual(self.s.context["checkout_amount"], "800")

    def test_changed_bank_details_prevent_repricing_against_an_old_card(self):
        self.checkout("3")
        self.s.bot.merchant["account"] = "4000000000000002"
        plan = self.s.turn("хочу дві")
        self.assertFalse(plan.on_send.get('handoff_now'))
        self.assertEqual(self.s.context["checkout_amount"], "800")

    def test_multi_item_quantity_change_asks_target_and_keeps_other_item(self):
        self.s.turn("Хочу штани M чорні і футболку L білу")
        self.s.turn("2")
        old = copy.deepcopy(self.s.context["invoice_items"])
        plan = self.s.turn("хочу дві")
        self.assertIn("Для якої речі", plan.text)
        self.assertEqual(self.s.context["invoice_items"], old)
        plan = self.s.turn("футболку")
        self.assertEqual([item["quantity"] for item in self.s.context["invoice_items"]], [1, 2])
        self.assertEqual(self.s.context["invoice_total"], "1800")
        self.assertEqual(self.s.context["cart"][0]["quantity"], 1)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_changed_quantity_invalidates_the_old_invoice_hash(self):
        self.checkout()
        old = invoice_key(snapshot(self.s.catalog, self.s.context))
        self.s.turn("давай дві")
        self.assertNotEqual(invoice_key(snapshot(self.s.catalog, self.s.context)), old)

    def test_payment_amount_and_method_are_not_item_quantities(self):
        for text in ("хочу 100 грн передоплати", "хочу 2 варіант"):
            self.assertIsNone(requested_quantity(text))

    def test_other_customer_context_does_not_share_quantity_or_personal_data(self):
        self.checkout()
        self.s.turn(NAME)
        second = self.s.bot.plan(["Хочу білу L"], {}, "102")
        self.assertNotIn("shipping_details", second.context)
        self.assertEqual(second.context["product"], "gallery-tee")
        self.s.turn("хочу дві")
        self.assertNotIn("quantity", second.context)


class WorkerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.s = journey.JourneyTests()
        self.s.setUp()
        self.addCleanup(self.s.doCleanups)

    def test_real_worker_does_not_enter_human_mode_for_random_photo_or_cod_quantity_change(self):
        s = self.s
        s.turn("Хочу замовити чорні M")
        s.turn("2")
        s.turn("", attachment=True)
        s.turn(NAME)
        s.turn("бля, та давай всьо разом")
        answer = s.turn("передумав, хочу дві замовити")
        self.assertIn("1600", answer)
        self.assertNotIn("скасовано", answer)
        self.assertEqual(s.worker.handoffs.get("account", "t")["mode"], "bot")
        context = s.store.context("account", "t", "buyer", "101", 30)
        self.assertEqual(context["invoice_items"][0]["quantity"], 2)
        self.assertFalse(context.get("receipt_submitted"))
        self.assertTrue(context.get("unclassified_attachment"))
        self.assertIn("1600", s.worker.orders.text("account", "t", "Панель"))

    def test_worker_rebuild_does_not_double_an_additional_quantity(self):
        s = self.s
        s.turn("Хочу замовити чорні M")
        s.turn("2")
        s.prepare("додай ще одну")
        s.stock.values[("suvene-pants", "L", "WHITE")] = 1
        before = len(s.client.sent)
        s.worker.send_approved()
        self.assertEqual(len(s.client.sent), before)
        s.worker.build()
        s.worker.send_approved()
        context = s.store.context("account", "t", "buyer", "101", 30)
        self.assertEqual(context["quantity"], 2)


if __name__ == "__main__":
    unittest.main()
