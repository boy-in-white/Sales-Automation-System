"""Complete customer conversations using only fake stock and test payment details."""

import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from catalog import Catalog
from dialogue import Dialogue
from inventory import DemoInventory
from checkout import DETAILS, shipping_details

TEST_CARD = "4111111111111111"
ROOT = Path(__file__).resolve().parents[1]


class Stock(DemoInventory):
    def __init__(self):
        self.values, self.calls = {}, []

    def rows(self, product):
        self.calls.append(product["key"])
        return [dict(row, available=self.values.get((product["key"], row["size_code"], row["color_code"]), 2))
                for row in super().rows(product)]


class ConsultationTests(unittest.TestCase):
    def setUp(self):
        data = json.loads((ROOT / "shop.json").read_text())
        for product, advert, price in zip(data["products"], (101, 102), (800, 500)):
            product.update(advert_ids=[advert], ledgerly_product_id=product["key"], price_uah=price)
        self.catalog, self.stock = Catalog(data), Stock()
        self.bot = Dialogue(self.catalog, self.stock, {"enabled": True, "kind": "card", "account": TEST_CARD})
        self.context = {}

    def turn(self, text, advert="101", attachment=False):
        plan = self.bot.plan([text], self.context, advert, has_attachments=attachment)
        self.context = dict(plan.context, **plan.on_send)
        self.assertLessEqual(len(plan.text), 2000)
        self.assertLessEqual(plan.text.count("😊"), 1)
        return plan

    def order(self, advert="101"):
        return self.turn("Хочу замовити чорні M", advert)

    def test_exact_pants_measurements_do_not_become_body_height(self):
        plan = self.turn("Які заміри Suvene S і M?")
        self.assertIn("104", plan.text)
        self.assertIn("106", plan.text)
        self.assertIn("пояс — 68", plan.text)
        self.assertNotIn("height", self.context)
        self.assertEqual(self.stock.calls, [])

    def test_specific_tee_measurements_and_excluded_xl(self):
        plan = self.turn("Яка ширина футболки M?", "102")
        self.assertIn("45", plan.text)
        self.assertIn("65", plan.text)
        self.assertEqual(self.stock.calls, [])
        plan = self.turn("Заміри XL?", "102")
        self.assertIn("не продаємо", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_seller_s_height_anchor_is_used_without_inventing_weight_ranges(self):
        plan = self.turn("зріст 175 вага 68")
        self.assertEqual(self.context["size"], "S")
        self.assertIn("Вам підійде S", plan.text)
        self.assertNotIn("кілограм", plan.text)
        self.assertNotIn("зростом і вагою", plan.text)

    def test_tee_can_be_compared_to_a_real_garment(self):
        self.catalog.products['gallery-tee']['sizing_basis'] = 'body'
        self.catalog.products['gallery-tee'].pop('height_guidance',None)
        first = self.turn("Який розмір на зріст 175 вагу 68?", "102")
        self.assertEqual(self.context["awaiting"], "usual_size")
        self.assertNotIn("handoff_now", first.on_send)
        second = self.turn("45/65", "102")
        self.assertEqual(self.context["size"], "M")
        self.assertIn("наявності", second.text)
        self.assertNotIn("параметр", second.text)

    def test_requested_size_is_not_a_claim_about_fit(self):
        plan = self.turn("Мені потрібен M розмір")
        self.assertNotIn("підійде", plan.text)
        self.assertNotIn("параметр", plan.text)

    def test_other_color_is_offered_and_only_applied_after_acceptance(self):
        self.stock.values.update({("suvene-pants", "L", "GRAY"): 0, ("suvene-pants", "L", "WHITE"): 0,
                                  ("suvene-pants", "M", "GRAY"): 0})
        plan = self.turn("Хочу сірі L")
        self.assertIn("чорний", plan.text)
        self.assertEqual(self.context["color"], "GRAY")
        plan = self.turn("Так")
        self.assertEqual((self.context["size"], self.context["color"]), ("L", "BLACK"))
        self.assertIn("checkout_offered", plan.on_send)

    def test_smaller_suvene_is_offered_by_its_real_size(self):
        self.stock.values.update({("suvene-pants", "M", color): 0 for color in ("GRAY", "BLACK", "WHITE")})
        plan = self.turn("Є сірі M?")
        self.assertIn("S", plan.text)
        self.assertIn("104", plan.text)
        self.assertIn("немає в наявності", plan.text)
        self.assertNotIn("Так,", plan.text)
        self.assertEqual(self.context["size"], "M")
        self.turn("Так")
        self.assertEqual(self.context["size"], "S")

    def test_multi_product_attributes_are_not_swapped(self):
        plan = self.turn("Хочу чорні штани M і білу футболку L")
        cart = self.context["cart"]
        self.assertEqual([(r["product"], r["size"], r["color"]) for r in cart],
                         [("suvene-pants", "M", "BLACK"), ("gallery-tee", "L", "WHITE")])
        self.assertTrue(plan.on_send["checkout_offered"])
        self.assertIn("1300", plan.text)
        self.assertEqual(set(plan.guard["stocks"]), {"suvene-pants", "gallery-tee"})
        self.assertEqual(self.stock.calls.count("suvene-pants"), 1)
        self.assertEqual(self.stock.calls.count("gallery-tee"), 1)

    def test_two_variants_of_one_product_are_separate(self):
        self.turn("Хочу штани M чорні і L сірі")
        self.assertEqual([(r["size"], r["color"]) for r in self.context["cart"]], [("M", "BLACK"), ("L", "GRAY")])

    def test_missing_cart_attributes_are_collected_for_the_correct_item(self):
        self.turn("Хочу штани M і футболку L")
        self.turn("чорні")
        self.assertEqual(self.context["cart"][0]["color"], "BLACK")
        self.assertIsNone(self.context["cart"][1]["color"])
        plan = self.turn("біла")
        self.assertEqual(self.context["cart"][1]["color"], "WHITE")
        self.assertTrue(plan.on_send["checkout_offered"])

    def test_cart_quantity_exceeding_stock_is_not_confirmed(self):
        self.stock.values[("gallery-tee", "L", "WHITE")] = 1
        plan = self.turn("Хочу штани M чорні і 2 футболки L білі")
        self.assertIn("немає", plan.text)
        self.assertNotIn("Усі вибрані речі є", plan.text)
        self.assertNotIn("checkout_offered", plan.on_send)

    def test_invalid_cart_quantity_is_not_silently_changed_to_one(self):
        for quantity in ("0", "-1", "1.5"):
            self.context = {}
            plan = self.turn("Хочу штани M чорні і %s футболки L білі" % quantity)
            self.assertNotIn("checkout_offered", plan.on_send)
            self.assertNotIn("Усі вибрані речі є", plan.text)

    def test_same_variant_quantities_are_combined(self):
        self.turn("Хочу штани M чорні і футболку L білу")
        plan = self.turn("Додай ще футболку L білу")
        self.assertEqual(len(self.context["cart"]), 2)
        self.assertEqual(self.context["cart"][1]["quantity"], 2)
        self.assertIn("1800", plan.text)

    def test_unscoped_cart_change_asks_which_product_without_changing_another(self):
        self.turn("Хочу штани M сірі і футболку L білу")
        before = copy.deepcopy(self.context["cart"])
        self.turn("Чорні M")
        self.assertEqual(self.context["cart"], before)
        self.assertEqual(self.context["awaiting"], "cart_target")
        self.turn("Штани")
        self.assertEqual(self.context["cart"][0]["color"], "BLACK")
        self.assertEqual(self.context["cart"][1], before[1])

    def test_removing_one_variant_does_not_remove_another(self):
        self.turn("Хочу штани M чорні і L сірі")
        self.turn("Прибери штани L сірі")
        self.assertEqual(len(self.context["cart"]), 1)
        self.assertEqual((self.context["cart"][0]["size"], self.context["cart"][0]["color"]), ("M", "BLACK"))

    def test_cod_sends_receipt_request_and_shipping_template_before_takeover(self):
        self.order()
        plan = self.turn("Післяплата")
        self.assertIn("100 грн", plan.text)
        self.assertIn("4111 1111 1111 1111", plan.text)
        self.assertIn("квитанц", plan.text)
        self.assertIn("ПІБ", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)
        self.assertEqual(self.context["checkout_stage"], "details")
        plan = self.turn("Петренко Іван Іванович\n0671234567\nЛьвів, Львівська область\nВідділення №12")
        self.assertEqual(self.context["shipping_details"]["phone"], "+380671234567")
        self.assertEqual(self.context["shipping_details"]["branch"], "Відділення №12")
        self.assertNotIn("handoff_now", plan.on_send)
        self.assertIn("Усе правильно?", plan.text)
        self.assertRegex(self.turn("так").text, "Відправка|Постараємося відправити")
        plan = self.turn("Квитанція", attachment=True)
        self.assertFalse(plan.on_send.get("handoff_now"))
        self.assertTrue(self.context["receipt_submitted"])
        self.assertNotIn("Оплату підтверджено", plan.text)

    def test_receipt_first_then_details_one_field_at_a_time(self):
        self.order()
        self.turn("2")
        plan = self.turn("Квитанція", attachment=True)
        self.assertIn("ПІБ", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)
        self.turn("Петренко Іван Іванович")
        self.turn("0671234567")
        self.turn("Львів, Львівська область")
        plan = self.turn("12")
        self.assertNotIn("handoff_now", plan.on_send)
        plan = self.turn("так")
        self.assertFalse(plan.on_send.get("handoff_now"))
        self.assertEqual(self.context["checkout_stage"], "submitted")
        self.assertEqual(len(self.context["shipping_details"]), 4)

    def test_full_prepayment_uses_item_total(self):
        self.order()
        plan = self.turn("Повна передоплата")
        self.assertIn("800 грн", plan.text)
        self.assertNotIn("100 грн", plan.text)
        self.assertIn("лише доставку", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_full_prepayment_for_cart_uses_all_quantities(self):
        self.turn("Хочу штани M чорні і 2 футболки L білі")
        plan = self.turn("3")
        self.assertIn("1800 грн", plan.text)

    def test_full_prepayment_preserves_kopecks(self):
        self.catalog.products["suvene-pants"]["price_uah"] = 800.95
        self.catalog.products["gallery-tee"]["price_uah"] = 500.75
        self.turn("Хочу штани M чорні і 2 футболки L білі")
        self.assertIn("1802.45 грн", self.turn("3").text)

    def test_unknown_total_is_not_invented(self):
        self.catalog.products["suvene-pants"]["price_uah"] = None
        self.order()
        plan = self.turn("3")
        self.assertFalse(plan.on_send.get('handoff_now'))
        self.assertNotIn("4111", plan.text)

    def test_olx_waits_for_customer_checkout_and_does_not_send_card(self):
        self.order()
        plan = self.turn("Олх доставка")
        self.assertIn("очікую оформлення", plan.text)
        self.assertNotIn("4111", plan.text)
        self.assertFalse(plan.on_send.get("handoff_now"))

    def test_negotiation_without_advance_does_not_send_card(self):
        self.order()
        plan = self.turn("Можна наложкою без передоплати?")
        self.assertIn("100 грн", plan.text)
        self.assertNotIn("4111", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_two_delivery_methods_need_clarification(self):
        self.order()
        plan = self.turn("ОЛХ чи наложка?")
        self.assertNotIn("4111", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)

    def test_delivery_question_is_not_mistaken_for_a_choice(self):
        self.order()
        plan = self.turn("Як працює післяплата?")
        self.assertNotIn("4111", plan.text)
        self.assertNotIn("handoff_now", plan.on_send)
        self.assertIn("100 грн", plan.text)
        self.assertIn("4111", self.turn("Так").text)

    def test_delivery_choice_and_dispatch_question_are_both_answered(self):
        self.order()
        self.bot.now = lambda: datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
        plan = self.turn("Наложкою, коли відправите?")
        self.assertIn("4111", plan.text)
        self.assertIn("сьогодні протягом дня", plan.text)

    def test_unavailable_blue_is_recognized_without_claiming_black_is_blue(self):
        plan = self.turn("Є сині M?")
        self.assertIn("синій", plan.text.lower())
        self.assertIn("не в наявності", plan.text)
        self.assertIn("чорний", plan.text)

    def test_blank_shipping_template_is_not_treated_as_real_data(self):
        self.assertEqual(shipping_details(DETAILS, {}), {})

    def test_payment_amount_cannot_be_mistaken_for_branch_number(self):
        known = {"name": "Петренко Іван Іванович", "phone": "+380671234567", "place": "Львів"}
        self.assertNotIn("branch", shipping_details("Оплатив 100 грн", known, "branch"))

    def test_weekday_and_weekend_dispatch_use_kyiv_calendar(self):
        for day, expected in ((6, "сьогодні протягом дня"), (7, "з понеділка"), (8, "з понеділка")):
            self.bot.now = lambda day=day: datetime(2026, 11, day, 12, tzinfo=timezone.utc)
            plan = self.turn("Коли відправите?")
            self.assertIn(expected, plan.text)
            self.assertIn("shipping_day", plan.guard)
            self.assertEqual(self.stock.calls, [])
        self.bot.now = lambda: datetime(2026, 11, 8, 23, tzinfo=timezone.utc)
        self.assertIn("сьогодні протягом дня", self.turn("Коли відправите?").text)

    def test_unknown_messages_get_contextual_help_without_handoff(self):
        self.assertNotIn("handoff_now", self.turn("незрозуміле звернення").on_send)
        self.assertFalse(self.turn("все ще незрозуміле звернення").on_send.get('handoff_now'))

    def test_payment_cancellation_does_not_request_more_personal_data(self):
        self.order()
        self.turn("2")
        plan = self.turn("Не треба")
        self.assertIn("скасовано", plan.text)
        self.assertNotIn("shipping_details", self.context)


if __name__ == "__main__":
    unittest.main()
