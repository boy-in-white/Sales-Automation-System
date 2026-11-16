import unittest

from parser import (
    extract_cost_text,
    extract_ttns,
    parse_message,
    safe_eval_cost_expr,
)
from tracking_notify import build_employee_text, sanitize_description


class ParserTests(unittest.TestCase):
    def test_unlabelled_product_and_price_from_order_card(self):
        cases = (
            ("Штани suvene сірі S", "1134", "20400560097167"),
            ("Штани suvene чорні S", "1134", "0504947958303"),
            ("Gallery dept чорна M", "650", "0504934357335"),
        )
        for product, price, ttn in cases:
            with self.subTest(product=product):
                parsed = parse_message(f"ФОП\n{ttn}\n{product}\n{price} грн")
                self.assertEqual(parsed["description"], product)
                self.assertEqual(parsed["product_name"], product)
                self.assertEqual(parsed["sale_price"], price)

    def test_price_is_not_a_product_and_multiple_product_lines_are_preserved(self):
        self.assertNotIn("description", parse_message("ФОП\n0504947958303\n1134 грн"))
        parsed = parse_message("ФОП\n0504947958303\nШтани Suvene чорні S\nФутболка Gallery Dept біла M\n1784 грн")
        self.assertEqual(parsed["description"], "Штани Suvene чорні S\nФутболка Gallery Dept біла M")

    def test_bare_price_can_never_be_rendered_as_description(self):
        self.assertEqual(sanitize_description("1134 грн"), "")
        card = build_employee_text(
            client_name="Denchik",
            user_id=1080531743,
            shipment_kind="ФОП",
            description="1134 грн",
            cost_text="1134",
            ttns=["20400560097167"],
        )
        self.assertNotIn("Опис: 1134", card)
        self.assertIn("Ціна продажу: 1134", card)

    def test_typical_private_message(self):
        text = (
            "Іван Петренко\n"
            "Чохол для iPhone\n"
            "Пошта: 990+100\n"
            "OLX ID: ABC-1234\n"
            "20 4514 2425 2955"
        )
        parsed = parse_message(text)
        self.assertEqual(extract_ttns(text), ["20451424252955"])
        self.assertEqual(parsed["cost"], "1090")
        self.assertEqual(parsed["description"], "Чохол для iPhone")
        self.assertEqual(parsed["olx_order_id"], "ABC-1234")

    def test_ttn_is_not_misread_as_cost(self):
        text = "20 4514 2425 2955"
        self.assertIsNone(extract_cost_text(text))
        self.assertNotIn("cost", parse_message(text))

    def test_cost_expression_is_bounded(self):
        self.assertEqual(safe_eval_cost_expr("990 + 100"), "1090")
        self.assertEqual(safe_eval_cost_expr("1200/3"), "400")
        self.assertIsNone(safe_eval_cost_expr("2**100"))
        self.assertIsNone(safe_eval_cost_expr("100/0"))
        self.assertIsNone(safe_eval_cost_expr("__import__('os')"))

    def test_multiple_ttns_are_deduplicated(self):
        text = "20451424252955\n20451424252955\n5900000000000"
        self.assertEqual(extract_ttns(text), ["20451424252955", "5900000000000"])

    def test_business_fields(self):
        parsed = parse_message(
            "Товар: штани SUVENE\nЦіна: 1200\nСобівартість: 500\n"
            "Доставка: 100\nКомісія: 50\nІнші витрати: 20\nКількість: 2\nОфлайн"
        )
        self.assertEqual(parsed["product_name"], "штани SUVENE")
        self.assertEqual(parsed["sale_price"], "1200")
        self.assertEqual(parsed["purchase_cost"], "500")
        self.assertEqual(parsed["delivery_cost"], "100")
        self.assertEqual(parsed["platform_fee"], "50")
        self.assertEqual(parsed["other_cost"], "20")
        self.assertEqual(parsed["quantity"], "2")
        self.assertEqual(parsed["shipment_kind"], "Офлайн")


if __name__ == "__main__":
    unittest.main()

