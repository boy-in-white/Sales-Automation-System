"""Product-specific FAQ and dispatch detours across real dialogue stages."""

import copy
import unittest
from datetime import datetime, timezone

import test_consultation_flow as consultation
import test_sales_journey as journey
from app import AppError
from catalog import Catalog
from product_registry import runtime_catalog

ADDRESS = "Коваленко Олег Петрович\n0671234567\nЛьвів, Львівська область\n12"


class ProductAdviceTests(unittest.TestCase):
    def setUp(self):
        self.s = consultation.ConsultationTests()
        self.s.setUp()

    def test_material_answer_uses_the_sellers_exact_composition(self):
        plan = self.s.turn("Який матеріал?")
        self.assertIn("бавовни", plan.text)
        self.assertIn("поліестеру", plan.text)
        self.assertIn("60%", plan.text)
        self.assertIn("40%", plan.text)
        self.assertNotIn("фліс", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)
        self.assertEqual(self.s.stock.calls, [])

    def test_fabric_and_composition_synonyms_receive_a_short_material_answer(self):
        for text in ("Яка тканина?", "А склад?", "коттон чи синтетика?"):
            plan = self.s.turn(text)
            self.assertIn("поліестеру", plan.text)
            self.assertNotIn("легкий фліс", plan.text)
            self.assertNotIn("handoff_now", plan.on_send)

    def test_waist_elasticity_is_not_claimed_for_the_garment_fabric(self):
        plan = self.s.turn("сама тканина тягнеться?")
        self.assertIn("еластичність самої тканини", plan.text)
        self.assertNotIn("пояс добре тягнеться", plan.text)
        self.assertFalse(plan.on_send.get('handoff_now'))

    def test_new_cart_action_clears_an_abandoned_fact_clarification(self):
        self.s.turn("Хочу штани M чорні і футболку L білу")
        self.s.turn("Який матеріал?")
        self.assertTrue(self.s.context["advice_pending"])
        self.s.turn("Додай ще футболку L білу")
        self.assertNotIn("advice_pending", self.s.context)
        self.assertEqual(self.s.context["cart"][1]["quantity"], 2)

    def test_waist_stretch_and_drawstring_are_answered_together(self):
        for text in ("Пояс сильно тягнеться?", "резинка тянется?", "А шнурки є?", "Добре розтягуються?"):
            plan = self.s.turn(text)
            self.assertIn("пояс добре тягнеться", plan.text)
            self.assertIn("шнурок", plan.text)
            self.assertNotIn("handoff_now", plan.on_send)
        self.assertEqual(self.s.stock.calls, [])

    def test_pilling_questions_in_customer_languages_use_the_supplied_fact(self):
        for text in ("Катишки з'являються?", "а катики не будуть?", "катышки бывают?", "Чи кошлатяться після прання?"):
            plan = self.s.turn(text)
            self.assertIn("не кошлатиться", plan.text)
            self.assertNotIn("handoff_now", plan.on_send)

    def test_actual_complaint_is_not_answered_with_a_sales_claim(self):
        plan = self.s.turn("Я вже отримав, після прання з'явилися катишки")
        self.assertFalse(plan.on_send.get('handoff_now'))
        self.assertNotIn("катишки не з'являються", plan.text)

    def test_lining_question_does_not_dump_other_facts(self):
        plan = self.s.turn("Вони з утепленням?")
        self.assertIn("легким флісом", plan.text)
        self.assertNotIn("бавовн", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_exists_in_a_fabric_question_does_not_cause_an_inventory_read(self):
        plan = self.s.turn("Є легкий фліс?")
        self.assertIn("легким флісом", plan.text)
        self.assertEqual(self.s.stock.calls, [])

    def test_payment_method_and_material_question_are_answered_in_the_same_turn(self):
        self.s.order()
        plan = self.s.turn("Наложкою, а який матеріал?")
        self.assertIn("поліестеру", plan.text)
        self.assertIn("4111", plan.text)
        self.assertEqual(self.s.context["checkout_method"], "cod")
        self.assertNotIn("handoff_now", plan.on_send)

    def test_new_multi_item_order_with_a_fact_question_does_not_drop_the_question(self):
        plan = self.s.turn("Хочу штани M чорні і футболку L білу, чи буде торг?")
        self.assertIn("без торгу", plan.text)
        self.assertEqual(len(self.s.context["cart"]), 2)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_one_requested_season_gets_only_that_season(self):
        for text, yes, others in (("Чи підійдуть на зиму?", "на зиму", ("весну", "осінь")),
                                  ("На весну норм?", "на весну", ("зиму", "осінь")),
                                  ("На осінь підійде?", "на осінь", ("весну", "зиму")),
                                  ("Зимой можно носить?", "на зиму", ("весну", "осінь"))):
            self.s.context = {}
            plan = self.s.turn(text)
            self.assertIn("Так, " + yes + " підійдуть", plan.text)
            for other in others:
                self.assertNotIn(other, plan.text)
            self.assertNotIn("зріст", plan.text)
            self.assertNotIn("handoff_now", plan.on_send)
        self.assertEqual(self.s.stock.calls, [])

    def test_general_season_question_lists_supported_seasons(self):
        plan = self.s.turn("На який сезон розраховані?")
        for word in ("весну", "осінь", "зиму", "фліс"):
            self.assertIn(word, plan.text)
        self.assertNotIn("літо", plan.text)

    def test_summer_is_not_advertised_as_another_supported_season(self):
        plan = self.s.turn("Влітку не буде жарко?")
        self.assertIn("затеплими", plan.text)
        self.assertNotIn("Так, на літо", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_material_and_waist_question_answers_both_without_repeating_the_description(self):
        plan = self.s.turn("Який матеріал і чи тягнеться пояс?")
        self.assertEqual(plan.text.count("бавовни"), 1)
        self.assertEqual(plan.text.count("шнурок"), 1)
        self.assertNotIn("фліс", plan.text)

    def test_no_bargaining_does_not_handoff_or_change_agreed_amount(self):
        self.s.order()
        self.s.turn("3")
        before = copy.deepcopy(self.s.context)
        for text in ("Торг є?", "Знижка буде?", "Остання ціна?", "За 450 віддасте?"):
            plan = self.s.turn(text)
            self.assertIn("без торгу", plan.text)
            self.assertNotIn("handoff_now", plan.on_send)
            for key in ("invoice_id", "invoice_items", "checkout_amount", "shipping_details"):
                self.assertEqual(self.s.context.get(key), before.get(key))
            self.assertNotIn("4111", plan.text)

    def test_aktualno_greets_and_checks_real_stock(self):
        plan = self.s.turn("актуально?")
        self.assertIn("Добрий день!", plan.text)
        self.assertIn("Так, актуально", plan.text)
        self.assertIn("stock", plan.guard)
        self.assertEqual(self.s.stock.calls, ["suvene-pants"])

    def test_aktualno_does_not_claim_stock_if_every_variant_is_empty(self):
        for size in ("S", "M", "L", "XL"):
            for color in ("BLACK", "GRAY", "WHITE"):
                self.s.stock.values[("suvene-pants", size, color)] = 0
        plan = self.s.turn("актуально?")
        self.assertNotIn("Так, актуально", plan.text)
        self.assertIn("немає", plan.text)

    def test_tee_does_not_inherit_pants_fleece_material_or_seasons(self):
        self.assertIn("100% бавовна", self.s.turn("Який матеріал?", "102").text)
        for text in ("Фліс є?", "На зиму підійде?"):
            self.s.context = {}
            plan = self.s.turn(text, "102")
            self.assertFalse(plan.on_send.get('handoff_now'))
            self.assertNotIn("Суміш бавовни", plan.text)
            self.assertNotIn("утеплені легким флісом", plan.text)
            self.assertNotIn("Так, на зиму", plan.text)

    def test_sensitive_suvene_facts_are_only_answered_when_asked(self):
        for text in ("Привіт", "актуально?", "Який матеріал?", "Торг є?", "На зиму підійде?"):
            self.s.context = {}
            plan = self.s.turn(text)
            for hidden in ("висока", "великомір", "−10", "+15", "без розтягування", "на поверхні"):
                self.assertNotIn(hidden, plan.text)

    def test_waist_measurement_is_flat_and_relaxed_only_on_direct_question(self):
        plan = self.s.turn("Ширина пояса це обхват чи замір без розтягування?")
        self.assertIn("ширина", plan.text)
        self.assertIn("без розтягування", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_suvene_cut_rise_and_size_fit_are_separate_answers(self):
        for text, answer in (("Який крій?", "baggy"), ("Посадка висока?", "Посадка висока"), ("Вони маломірять?", "великомірить")):
            self.s.context = {}
            plan = self.s.turn(text)
            self.assertIn(answer, plan.text)
            self.assertNotIn("handoff_now", plan.on_send)
        plan = self.s.turn("Який крій?")
        self.assertNotIn("Посадка висока", plan.text)

    def test_temperature_is_answered_without_claiming_a_different_product_has_the_same_range(self):
        plan = self.s.turn("При якій температурі комфортно носити?")
        self.assertIn("−10", plan.text)
        self.assertIn("+15", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)
        self.s.context = {}
        plan = self.s.turn("Яка комфортна температура?", "102")
        self.assertNotIn("−10", plan.text)

    def test_suvene_print_is_embroidery_not_a_guess_about_the_tee(self):
        plan = self.s.turn("А принт не зітреться?")
        self.assertIn("вишивкою", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)
        self.s.context = {}
        plan = self.s.turn("Який принт?", "102")
        self.assertNotIn("вишивкою", plan.text)
        self.assertFalse(plan.on_send.get('handoff_now'))

    def test_tee_is_cotton_relaxed_and_does_not_show_through(self):
        for text, answer in (("Який матеріал?", "100% бавовна"), ("Чи просвічується біла?", "не просвічуються"), ("Яка посадка?", "relaxed")):
            self.s.context = {}
            plan = self.s.turn(text, "102")
            self.assertIn(answer, plan.text)
            self.assertNotIn("handoff_now", plan.on_send)

    def test_tee_xl_is_not_sold_even_if_stock_provider_returns_it(self):
        self.s.stock.values[("gallery-tee", "XL", "WHITE")] = 10
        plan = self.s.turn("Є XL біла?", "102")
        self.assertIn("не продаємо", plan.text)
        self.assertNotIn("є в наявності", plan.text)
        self.s.context = {}
        plan = self.s.turn("актуально?", "102")
        self.assertNotIn("XL", plan.text)

    def test_checkout_does_not_issue_an_invoice_for_a_legacy_excluded_xl_selection(self):
        self.s.context = {"product": "gallery-tee", "size": "XL", "color": "WHITE", "quantity": 1, "checkout_offered": True}
        self.s.stock.values[("gallery-tee", "XL", "WHITE")] = 10
        plan = self.s.turn("2", "102")
        self.assertNotIn("4111", plan.text)
        self.assertFalse(self.s.context.get("invoice_issued"))

    def test_facts_do_not_interrupt_pending_size_selection(self):
        self.s.turn("Який розмір підійде?")
        before = self.s.context["awaiting"]
        plan = self.s.turn("а який матеріал?")
        self.assertIn("поліестеру", plan.text)
        self.assertEqual(self.s.context["awaiting"], before)
        self.assertNotIn("handoff_now", plan.on_send)
        self.assertIn("наявності", self.s.turn("зріст 175 вага 68").text)

    def test_facts_during_checkout_preserve_invoice_and_do_not_become_a_name(self):
        self.s.order()
        self.s.turn("2")
        before = copy.deepcopy(self.s.context)
        for text in ("який матеріал", "Пояс тягнеться?", "підійдуть на зиму?", "Торг є?"):
            plan = self.s.turn(text)
            self.assertNotIn("handoff_now", plan.on_send)
            for key in ("invoice_id", "invoice_items", "checkout_amount", "shipping_details", "awaiting"):
                self.assertEqual(self.s.context.get(key), before.get(key))
        self.assertIn("Усе правильно?", self.s.turn(ADDRESS).text)

    def test_material_question_in_a_multi_product_cart_asks_which_item_then_resumes(self):
        self.s.turn("Хочу штани M чорні і футболку L білу")
        self.s.turn("2")
        before = copy.deepcopy(self.s.context["cart"])
        plan = self.s.turn("Який матеріал?")
        self.assertIn("Про яку річ", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)
        plan = self.s.turn("Штани")
        self.assertIn("поліестеру", plan.text)
        self.assertEqual(self.s.context["cart"], before)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_pure_discount_applies_to_the_shop_without_repeating_each_cart_item(self):
        self.s.turn("Хочу штани M чорні і футболку L білу")
        plan = self.s.turn("Торг буде?")
        self.assertEqual(plan.text.count("без торгу"), 1)
        self.assertNotIn("Про яку річ", plan.text)

    def test_repeated_dispatch_queries_are_calm_and_keep_progress(self):
        self.s.bot.now = lambda: datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
        self.s.order()
        self.s.turn("2")
        before = self.s.context["invoice_id"]
        for text in ("Коли відправите?", "ну коли вже відправите?", "ще довго?"):
            plan = self.s.turn(text)
            self.assertIn("сьогодні протягом дня", plan.text)
            self.assertNotIn("handoff_now", plan.on_send)
            self.assertIn("ще потрібні", plan.text)
            self.assertEqual(self.s.context["invoice_id"], before)
            self.assertIn("shipping_day", plan.guard)
        self.assertEqual(self.s.context["dispatch_queries"], 3)

    def test_weekend_and_followup_use_the_same_calendar_policy(self):
        self.s.bot.now = lambda: datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
        for text in ("Коли відправите?", "ну коли?"):
            plan = self.s.turn(text)
            self.assertIn("з понеділка", plan.text)
            self.assertNotIn("сьогодні протягом дня", plan.text)
            self.assertNotIn("handoff_now", plan.on_send)

    def test_actual_shipment_status_requires_evidence_instead_of_a_fabricated_sent_claim(self):
        for text in ("Вже відправили?", "Дайте ТТН", "Где моя посылка?", "Уже отправили?"):
            plan = self.s.turn(text)
            self.assertFalse(plan.on_send.get('handoff_now'))
            self.assertNotIn("сьогодні протягом дня", plan.text)
            self.assertNotIn("вже відправлено", plan.text)

    def test_material_and_dispatch_are_both_answered(self):
        self.s.bot.now = lambda: datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
        plan = self.s.turn("Який матеріал і коли відправите?")
        self.assertIn("поліестеру", plan.text)
        self.assertIn("сьогодні протягом дня", plan.text)

    def test_inventory_refresh_preserves_only_the_matching_products_facts(self):
        records = [{"id": "pants-id", "canonical_name": "Штани Suvene", "available_sizes": ["M"], "aliases": []},
                   {"id": "new-id", "canonical_name": "Штани іншої марки", "available_sizes": ["M"], "aliases": []}]
        updated = runtime_catalog(self.s.catalog, records, [])
        by_id = {p["ledgerly_product_id"]: p for p in updated.products.values()}
        self.assertIn("material", by_id["pants-id"]["facts"])
        self.assertNotIn("facts", by_id["new-id"])

    def test_bad_fact_schema_is_rejected(self):
        for facts in ({"seasons": ["winter", "winter"]}, {"material": 100}, {"seasons": ["unknown"]}, {"made_up": "fact"}):
            data = copy.deepcopy(self.s.catalog.data)
            data["products"][0]["facts"] = facts
            with self.assertRaises(AppError):
                Catalog(data)


class AdviceWorkerTests(unittest.TestCase):
    def setUp(self):
        self.s = journey.JourneyTests()
        self.s.setUp()
        self.addCleanup(self.s.doCleanups)

    def test_worker_keeps_automatic_mode_for_product_faq_and_preserves_the_invoice(self):
        s = self.s
        s.turn("Хочу замовити чорні M")
        s.turn("2")
        before = s.store.context("account", "t", "buyer", "101", 30)
        answer = s.turn("на зиму підійдуть?")
        self.assertIn("Так, на зиму", answer)
        self.assertEqual(s.worker.handoffs.get("account", "t")["mode"], "bot")
        after = s.store.context("account", "t", "buyer", "101", 30)
        self.assertEqual(after["invoice_id"], before["invoice_id"])
        self.assertEqual(after.get("shipping_details"), before.get("shipping_details"))

    def test_changed_fact_invalidates_an_unsent_answer_before_post(self):
        s = self.s
        s.prepare("який матеріал?")
        s.worker.catalog.products["suvene-pants"]["facts"]["material"] = "Оновлений склад для тесту."
        s.worker.send_approved()
        self.assertEqual(s.client.sent, [])
        s.worker.build()
        s.worker.send_approved()
        self.assertIn("Оновлений склад", s.client.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
