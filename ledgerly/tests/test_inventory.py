from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import business_db
import inventory


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.answers = []

    def send_message(self, chat_id, text, **kwargs):
        message = SimpleNamespace(
            message_id=len(self.sent) + 1,
            chat=SimpleNamespace(id=chat_id),
            text=text,
            reply_markup=kwargs.get("reply_markup"),
        )
        self.sent.append(message)
        return message

    def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)

    def answer_callback_query(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class InventoryDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        path = Path(self.temp_dir.name) / "inventory.sqlite3"
        self.old_database_url = business_db.BUSINESS_DATABASE_URL
        business_db.BUSINESS_DATABASE_URL = f"sqlite:///{path}"
        business_db.init_business_db()

    def tearDown(self):
        business_db.BUSINESS_DATABASE_URL = self.old_database_url
        self.temp_dir.cleanup()

    def test_product_has_only_selected_size_and_color_variants(self):
        product = business_db.create_inventory_product(
            "штани SUVENE",
            ["S", "M", "L"],
            ["BLACK", "WHITE"],
        )
        variants = business_db.list_product_variants(product["id"])

        self.assertEqual(len(variants), 6)
        self.assertEqual({row["size_code"] for row in variants}, {"S", "M", "L"})
        self.assertEqual({row["color_code"] for row in variants}, {"BLACK", "WHITE"})
        self.assertEqual(product["canonical_name"], "Штани Suvene")

    def test_batch_receipt_and_writeoff_are_audited(self):
        product = business_db.create_inventory_product(
            "Футболки Gallery dept",
            ["M"],
            ["GRAY"],
        )
        variant = business_db.list_product_variants(product["id"])[0]

        received = business_db.change_inventory_stock(
            variant["id"],
            5,
            108,
            "Owner",
            movement_type="RECEIPT",
        )
        remaining = business_db.change_inventory_stock(
            variant["id"],
            -1,
            108,
            "Owner",
            movement_type="WRITE_OFF",
        )

        self.assertEqual(received["on_hand"], 5)
        self.assertEqual(remaining["available"], 4)
        with business_db._transaction() as session:
            movements = session.all(
                "SELECT * FROM inventory_movements ORDER BY created_at, id"
            )
            stock_view = session.one(
                "SELECT * FROM v_inventory_stock WHERE variant_id = ?",
                (variant["id"],),
            )
        self.assertEqual([row["quantity_delta"] for row in movements], [5, -1])
        self.assertTrue(all(row["actor_user_id"] == 108 for row in movements))
        self.assertEqual(stock_view["product_name"], "Футболки Gallery Dept")
        self.assertEqual(stock_view["color_name"], "Сірий")
        self.assertEqual(stock_view["available"], 4)

    def test_stock_cannot_be_negative(self):
        product = business_db.create_inventory_product(
            "Штани Broken Planet",
            ["M"],
            ["BLACK"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        with self.assertRaises(ValueError):
            business_db.change_inventory_stock(variant["id"], -1, 108)

    def test_product_can_be_deleted_only_with_zero_stock_and_recreated(self):
        product = business_db.create_inventory_product(
            "Штани Suvene",
            ["M"],
            ["BLACK"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.change_inventory_stock(variant["id"], 2, 108, "Owner")

        with self.assertRaisesRegex(ValueError, "Спочатку спиши весь товар"):
            business_db.archive_inventory_product(product["id"])

        business_db.change_inventory_stock(variant["id"], -2, 108, "Owner")
        deleted = business_db.archive_inventory_product(product["id"])
        self.assertEqual(deleted["id"], product["id"])
        self.assertEqual(business_db.list_inventory_products(), [])

        recreated = business_db.create_inventory_product(
            "Штани Suvene",
            ["S", "M"],
            ["BLACK"],
        )
        self.assertEqual(recreated["id"], product["id"])
        self.assertEqual(len(business_db.list_product_variants(product["id"])), 2)

    def test_sea_and_air_are_batches_of_the_same_product(self):
        product = business_db.create_inventory_product(
            "Штани Suvene",
            ["M"],
            ["BLACK"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.set_product_transport_cost(product["id"], "SEA", "400", 108)
        business_db.set_product_transport_cost(product["id"], "AIR", "600", 108)

        sea = business_db.receive_inventory_batch(variant["id"], "SEA", 5, 108)
        business_db.set_product_transport_cost(product["id"], "SEA", "450", 108)
        air = business_db.receive_inventory_batch(variant["id"], "AIR", 2, 108)
        batches = business_db.list_inventory_batches(variant["id"])

        self.assertEqual(len(business_db.list_inventory_products()), 1)
        self.assertEqual(sea["unit_cost_cents"], 40000)
        self.assertEqual(air["unit_cost_cents"], 60000)
        self.assertEqual([row["unit_cost_cents"] for row in batches], [40000, 60000])
        self.assertEqual(business_db.get_inventory_variant(variant["id"])["on_hand"], 7)

    def test_fifo_batches_calculate_order_profit(self):
        product = business_db.create_inventory_product(
            "Штани Broken Planet",
            ["L"],
            ["GRAY"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.set_product_transport_cost(product["id"], "SEA", "400", 108)
        business_db.receive_inventory_batch(variant["id"], "SEA", 2, 108)
        business_db.set_product_transport_cost(product["id"], "AIR", "600", 108)
        business_db.receive_inventory_batch(variant["id"], "AIR", 2, 108)
        order = business_db.record_order(
            ttn="20451424252979",
            sale_type_label="ОЛХ",
            carrier="Nova Poshta",
            product_name="Штани Broken Planet сірі L",
            quantity=3,
            sale_price="2100",
            initial_status="DELIVERED",
        )

        result = business_db.mark_order_in_1c(order["id"], 108, "Owner")
        saved = business_db.get_order(order["id"])
        batches = business_db.list_inventory_batches(variant["id"])
        stats = business_db.get_sales_stats("all")

        self.assertTrue(result["profit_ready"])
        self.assertEqual(result["purchase_cost_cents"], 140000)
        self.assertEqual(result["profit_cents"], 70000)
        self.assertEqual(result["transport_modes"], ["SEA", "AIR"])
        self.assertEqual(saved["cost_source"], "FIFO_BATCH")
        self.assertEqual([row["remaining_quantity"] for row in batches], [0, 1])
        self.assertEqual(stats["profit_cents"], 70000)
        self.assertEqual(stats["cost_pending_count"], 0)

    def test_shipped_items_move_to_in_transit_and_leave_available_stock(self):
        product = business_db.create_inventory_product(
            "Штани Suvene",
            ["M"],
            ["BLACK"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.set_product_transport_cost(product["id"], "SEA", "400", 108)
        business_db.receive_inventory_batch(variant["id"], "SEA", 5, 108)
        order = business_db.record_order(
            ttn="20451424252981",
            sale_type_label="Наложка",
            carrier="Nova Poshta",
            product_name="Штани Suvene чорні M",
            quantity=5,
            sale_price="5000",
        )

        shipped = business_db.mark_order_shipped_by_ttn(order["ttn"], 108, "Owner")
        repeated = business_db.mark_order_shipped_by_ttn(order["ttn"], 108, "Owner")
        stock = business_db.get_inventory_variant(variant["id"])

        self.assertEqual(shipped["reserved_quantity"], 5)
        self.assertEqual(repeated["reservation_status"], "already_reserved")
        self.assertEqual(stock["on_hand"], 5)
        self.assertEqual(stock["reserved"], 5)
        self.assertEqual(stock["available"], 0)
        with business_db._transaction() as session:
            stock_view = session.one(
                "SELECT * FROM v_inventory_stock WHERE variant_id = ?",
                (variant["id"],),
            )
            in_transit = session.one(
                "SELECT * FROM v_inventory_in_transit WHERE order_id = ?",
                (order["id"],),
            )
        self.assertEqual(stock_view["in_transit"], 5)
        self.assertEqual(in_transit["quantity"], 5)

    def test_order_quantity_is_reserved_all_or_nothing(self):
        product = business_db.create_inventory_product(
            "Штани Suvene", ["M"], ["BLACK"]
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.change_inventory_stock(variant["id"], 1, 108, "Owner")
        order = business_db.record_order(
            ttn="20451424252984",
            sale_type_label="Наложка",
            carrier="Nova Poshta",
            product_name="Штани Suvene чорні M",
            quantity=2,
        )

        result = business_db.reserve_order_by_ttn(order["ttn"], 108, "Owner")

        self.assertEqual(result["reservation_status"], "shortage")
        self.assertEqual(result["reserved_quantity"], 0)
        self.assertEqual(business_db.get_inventory_variant(variant["id"])["reserved"], 0)

    def test_returned_item_leaves_in_transit_and_becomes_available_again(self):
        product = business_db.create_inventory_product(
            "Футболка Gallery Dept",
            ["S"],
            ["WHITE"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.change_inventory_stock(variant["id"], 1, 108, "Owner")
        order = business_db.record_order(
            ttn="20451424252982",
            sale_type_label="Наложка",
            carrier="Nova Poshta",
            product_name="Футболка Gallery Dept біла S",
        )
        business_db.mark_order_shipped_by_ttn(order["ttn"], 108, "Owner")

        business_db.update_order_status_by_ttn(order["ttn"], "REFUSED")
        refused_stock = business_db.get_inventory_variant(variant["id"])
        self.assertEqual(refused_stock["reserved"], 1)
        self.assertEqual(refused_stock["available"], 0)

        returned = business_db.update_order_status_by_ttn(order["ttn"], "RETURNED")
        returned_stock = business_db.get_inventory_variant(variant["id"])
        self.assertEqual(returned["reservation_status"], "released")
        self.assertEqual(returned_stock["reserved"], 0)
        self.assertEqual(returned_stock["available"], 1)

    def test_1c_consumes_in_transit_reservation_without_double_subtraction(self):
        product = business_db.create_inventory_product(
            "Штани Broken Planet",
            ["L"],
            ["GRAY"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.set_product_transport_cost(product["id"], "AIR", "600", 108)
        business_db.receive_inventory_batch(variant["id"], "AIR", 2, 108)
        order = business_db.record_order(
            ttn="20451424252983",
            sale_type_label="ОЛХ",
            carrier="Nova Poshta",
            product_name="Штани Broken Planet сірі L",
            quantity=1,
            sale_price="1100",
        )
        business_db.mark_order_shipped_by_ttn(order["ttn"], 108, "Owner")

        result = business_db.mark_order_in_1c(order["id"], 108, "Owner")
        stock = business_db.get_inventory_variant(variant["id"])

        self.assertEqual(result["deducted_quantity"], 1)
        self.assertEqual(stock["on_hand"], 1)
        self.assertEqual(stock["reserved"], 0)
        self.assertEqual(stock["available"], 1)
        with business_db._transaction() as session:
            reservation = session.one(
                "SELECT * FROM inventory_reservations WHERE order_id = ?",
                (order["id"],),
            )
        self.assertEqual(reservation["status"], "CONSUMED")

    def test_stats_do_not_fake_profit_without_cost(self):
        business_db.record_order(
            ttn="20451424252980",
            sale_type_label="Офлайн",
            carrier="Offline",
            product_name="Футболка без собівартості",
            sale_price="900",
            initial_status="DELIVERED",
        )

        stats = business_db.get_sales_stats("all")

        self.assertEqual(stats["turnover_cents"], 90000)
        self.assertEqual(stats["profit_cents"], 0)
        self.assertEqual(stats["cost_pending_count"], 1)

    def test_1c_confirmation_writes_exact_variant_off_once(self):
        product = business_db.create_inventory_product(
            "Штани Suvene",
            ["M"],
            ["BLACK"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.change_inventory_stock(variant["id"], 5, 108, "Owner")
        order = business_db.record_order(
            ttn="20451424252971",
            sale_type_label="ОЛХ",
            carrier="Nova Poshta",
            product_name="штани SUVENE чорні M",
            quantity=1,
            sale_price="900",
        )

        result = business_db.mark_order_in_1c(order["id"], 108, "Owner")
        repeated = business_db.mark_order_in_1c(order["id"], 108, "Owner")

        self.assertEqual(result["inventory_status"], "written_off")
        self.assertEqual(result["balance_after"], 4)
        self.assertEqual(
            business_db.get_order(order["id"])["product_id"], product["id"]
        )
        self.assertEqual(len(business_db.list_inventory_products()), 1)
        self.assertTrue(repeated["already_recorded"])
        self.assertEqual(
            business_db.get_inventory_variant(variant["id"])["on_hand"],
            4,
        )
        with business_db._transaction() as session:
            movements = session.all(
                "SELECT * FROM v_inventory_movements WHERE order_id = ?",
                (order["id"],),
            )
        self.assertEqual(len(movements), 1)
        self.assertEqual(movements[0]["quantity_delta"], -1)

    def test_1c_confirmation_reports_sold_out_without_blocking(self):
        product = business_db.create_inventory_product(
            "Футболка Gallery Dept",
            ["S"],
            ["WHITE"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.change_inventory_stock(variant["id"], 1, 108, "Owner")
        order = business_db.record_order(
            ttn="20451424252972",
            sale_type_label="Наложка",
            carrier="Nova Poshta",
            product_name="Gallery Dept футболка біла S",
        )

        result = business_db.mark_order_in_1c(order["id"], 108, "Owner")

        self.assertEqual(result["inventory_status"], "sold_out")
        self.assertEqual(result["balance_after"], 0)
        self.assertIn("закінчився", result["warning"])
        self.assertIsNotNone(business_db.get_order(order["id"])["onec_recorded_at"])

    def test_1c_confirmation_reports_shortage_and_keeps_zero_balance(self):
        product = business_db.create_inventory_product(
            "Штани Broken Planet",
            ["L"],
            ["GRAY"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        order = business_db.record_order(
            ttn="20451424252973",
            sale_type_label="ФОП",
            carrier="Nova Poshta",
            product_name="Штани Broken Planet сірі L",
            quantity=2,
        )

        result = business_db.mark_order_in_1c(order["id"], 108, "Owner")

        self.assertEqual(result["inventory_status"], "shortage")
        self.assertEqual(result["deducted_quantity"], 0)
        self.assertEqual(business_db.get_inventory_variant(variant["id"])["on_hand"], 0)
        self.assertIsNotNone(business_db.get_order(order["id"])["onec_recorded_at"])

    def test_variant_with_stock_cannot_be_disabled(self):
        product = business_db.create_inventory_product(
            "Штани Broken Planet",
            ["S", "M"],
            ["BLACK"],
        )
        medium = next(
            row
            for row in business_db.list_product_variants(product["id"])
            if row["size_code"] == "M"
        )
        business_db.change_inventory_stock(medium["id"], 5, 108)

        with self.assertRaisesRegex(ValueError, "обнули залишок"):
            business_db.configure_product_variants(
                product["id"],
                ["S"],
                ["BLACK"],
            )

    def test_wizard_survives_between_messages(self):
        business_db.save_inventory_wizard(
            108,
            108,
            "NEW_OPTIONS",
            {"name": "Штани Suvene", "sizes": ["S", "M"]},
        )
        wizard = business_db.get_inventory_wizard(108)
        self.assertEqual(wizard["step"], "NEW_OPTIONS")
        self.assertEqual(wizard["payload"]["sizes"], ["S", "M"])
        business_db.clear_inventory_wizard(108)
        self.assertIsNone(business_db.get_inventory_wizard(108))

    def test_telegram_wizard_creates_product_with_selected_sizes(self):
        bot = FakeBot()
        user = SimpleNamespace(id=108, full_name="Owner", username="owner")
        source_message = SimpleNamespace(
            message_id=1, chat=SimpleNamespace(id=108), text=""
        )

        inventory.handle_inventory_callback(
            bot,
            SimpleNamespace(
                id="add",
                data="inv:add:start",
                from_user=user,
                message=source_message,
            ),
        )
        inventory.handle_inventory_text(
            bot,
            SimpleNamespace(
                chat=SimpleNamespace(id=108),
                from_user=user,
                text="Штани Broken Planet",
            ),
        )
        options_message = bot.sent[-1]
        for size in ("S", "M", "L"):
            inventory.handle_inventory_callback(
                bot,
                SimpleNamespace(
                    id=f"size-{size}",
                    data=f"inv:toggle:size:{size}",
                    from_user=user,
                    message=options_message,
                ),
            )
        inventory.handle_inventory_callback(
            bot,
            SimpleNamespace(
                id="save",
                data="inv:save:new",
                from_user=user,
                message=options_message,
            ),
        )

        self.assertEqual(
            business_db.get_inventory_wizard(108)["step"],
            "NEW_COST_SEA",
        )
        inventory.handle_inventory_text(
            bot,
            SimpleNamespace(
                chat=SimpleNamespace(id=108),
                from_user=user,
                text="400",
            ),
        )
        self.assertEqual(
            business_db.get_inventory_wizard(108)["step"],
            "NEW_COST_AIR",
        )
        inventory.handle_inventory_text(
            bot,
            SimpleNamespace(
                chat=SimpleNamespace(id=108),
                from_user=user,
                text="600",
            ),
        )

        products = business_db.list_inventory_products()
        variants = business_db.list_product_variants(products[0]["id"])
        self.assertEqual(products[0]["canonical_name"], "Штани Broken Planet")
        self.assertEqual(len(variants), 9)
        self.assertEqual({row["size_code"] for row in variants}, {"S", "M", "L"})
        self.assertEqual(
            business_db.get_product_transport_costs(products[0]["id"]),
            {"SEA": 40000, "AIR": 60000},
        )
        self.assertIsNone(business_db.get_inventory_wizard(108))
        self.assertIn("Товар готовий", bot.sent[-1].text)

    def test_telegram_receipt_remembers_transport_cost_and_accepts_typed_quantity(self):
        product = business_db.create_inventory_product(
            "Штани Suvene",
            ["M"],
            ["BLACK"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        bot = FakeBot()
        user = SimpleNamespace(id=108, full_name="Owner", username="owner")
        source_message = SimpleNamespace(
            message_id=1, chat=SimpleNamespace(id=108), text=""
        )

        inventory.handle_inventory_callback(
            bot,
            SimpleNamespace(
                id="sea",
                data=f"inv:method:SEA:{variant['id']}",
                from_user=user,
                message=source_message,
            ),
        )
        self.assertEqual(
            business_db.get_inventory_wizard(108)["step"],
            "SET_RECEIPT_COST",
        )
        inventory.handle_inventory_text(
            bot,
            SimpleNamespace(
                chat=SimpleNamespace(id=108),
                from_user=user,
                text="400",
            ),
        )
        quantity_message = bot.sent[-1]
        self.assertEqual(business_db.get_inventory_wizard(108)["step"], "RECEIPT_QUANTITY")
        self.assertFalse(any(button.text.startswith("+") for row in quantity_message.reply_markup.keyboard for button in row))
        inventory.handle_inventory_text(
            bot, SimpleNamespace(chat=SimpleNamespace(id=108), from_user=user, text="5")
        )
        result_message = bot.sent[-1]
        add_more = next(b.callback_data for row in result_message.reply_markup.keyboard for b in row if b.text == "Додати ще")
        self.assertEqual(add_more, f"inv:receivep:x:{product['id']}")
        inventory.handle_inventory_callback(bot, SimpleNamespace(id="more", data=add_more, from_user=user, message=result_message))
        self.assertTrue(any(variant["id"] in b.callback_data for row in bot.edited[-1]["reply_markup"].keyboard for b in row))

        self.assertEqual(business_db.get_inventory_variant(variant["id"])["on_hand"], 5)
        self.assertEqual(
            business_db.get_product_transport_costs(product["id"])["SEA"],
            40000,
        )
        self.assertIn("Надходження додано", result_message.text)

    def test_back_navigation_disarms_old_quantity_and_legacy_plus_button_does_not_add(self):
        product = business_db.create_inventory_product("Футболка", ["M"], ["BLACK"])
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.set_product_transport_cost(product["id"], "SEA", "200", 108)
        bot = FakeBot()
        user = SimpleNamespace(id=108, full_name="Owner", username="owner")
        message = SimpleNamespace(message_id=1, chat=SimpleNamespace(id=108), text="")
        def click(data):
            inventory.handle_inventory_callback(bot, SimpleNamespace(id=data, data=data, from_user=user, message=message))
        click(f"inv:method:SEA:{variant['id']}")
        self.assertEqual(business_db.get_inventory_wizard(108)["step"], "RECEIPT_QUANTITY")
        click(f"inv:recvvar:x:{variant['id']}")
        self.assertIsNone(business_db.get_inventory_wizard(108))
        click(f"inv:receive:5:{variant['id']}:SEA")
        self.assertEqual(business_db.get_inventory_variant(variant["id"])["on_hand"], 0)
        self.assertEqual(business_db.get_inventory_wizard(108)["step"], "RECEIPT_QUANTITY")

    def test_telegram_manual_writeoff_does_not_change_turnover(self):
        product = business_db.create_inventory_product(
            "Футболка Gallery Dept",
            ["M"],
            ["WHITE"],
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.change_inventory_stock(variant["id"], 5, 108, "Owner")
        bot = FakeBot()
        user = SimpleNamespace(id=108, full_name="Owner", username="owner")
        source_message = SimpleNamespace(
            message_id=1, chat=SimpleNamespace(id=108), text=""
        )

        inventory.handle_inventory_callback(
            bot,
            SimpleNamespace(
                id="write-off",
                data=f"inv:writeoffq:x:{variant['id']}",
                from_user=user,
                message=source_message,
            ),
        )
        self.assertEqual(
            business_db.get_inventory_wizard(108)["step"],
            "WRITE_OFF_QUANTITY",
        )
        inventory.handle_inventory_text(
            bot,
            SimpleNamespace(
                chat=SimpleNamespace(id=108),
                from_user=user,
                text="3",
            ),
        )

        self.assertEqual(business_db.get_inventory_variant(variant["id"])["on_hand"], 2)
        self.assertEqual(business_db.get_sales_stats("all")["turnover_cents"], 0)
        self.assertIn("оберт не змінено", bot.sent[-1].text)
        with business_db._transaction() as session:
            movement = session.one(
                "SELECT * FROM inventory_movements ORDER BY created_at DESC, id DESC LIMIT 1"
            )
        self.assertEqual(movement["quantity_delta"], -3)
        self.assertEqual(movement["movement_type"], "WRITE_OFF")

    def test_last_receipt_can_be_undone_once_with_batch_history_kept(self):
        product = business_db.create_inventory_product(
            "Штани Suvene", ["M"], ["BLACK"]
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.set_product_transport_cost(product["id"], "SEA", "400", 108)
        receipt = business_db.receive_inventory_batch(
            variant["id"], "SEA", 5, 108, "Owner"
        )

        result = business_db.undo_inventory_movement(
            receipt["movement_id"], 108, "Owner"
        )

        self.assertEqual(result["on_hand"], 0)
        self.assertEqual(
            business_db.list_inventory_batches(variant["id"])[0]["remaining_quantity"],
            0,
        )
        with self.assertRaisesRegex(ValueError, "вже скасовано"):
            business_db.undo_inventory_movement(receipt["movement_id"], 108)
        with business_db._transaction() as session:
            movements = session.all(
                "SELECT movement_type, quantity_delta FROM inventory_movements "
                "WHERE variant_id = ? ORDER BY created_at, id",
                (variant["id"],),
            )
        self.assertEqual(
            [(row["movement_type"], row["quantity_delta"]) for row in movements],
            [("RECEIPT_SEA", 5), ("UNDO", -5)],
        )

    def test_undo_writeoff_restores_stock_and_fifo_batch(self):
        product = business_db.create_inventory_product(
            "Футболка Gallery Dept", ["L"], ["WHITE"]
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.set_product_transport_cost(product["id"], "AIR", "600", 108)
        business_db.receive_inventory_batch(variant["id"], "AIR", 5, 108)
        writeoff = business_db.change_inventory_stock(
            variant["id"],
            -2,
            108,
            "Owner",
            movement_type="WRITE_OFF",
        )
        self.assertEqual(
            business_db.list_inventory_batches(variant["id"])[0]["remaining_quantity"],
            3,
        )

        restored = business_db.undo_inventory_movement(
            writeoff["movement_id"], 108, "Owner"
        )

        self.assertEqual(restored["on_hand"], 5)
        self.assertEqual(
            business_db.list_inventory_batches(variant["id"])[0]["remaining_quantity"],
            5,
        )

    def test_receipt_undo_is_blocked_when_stock_is_reserved(self):
        product = business_db.create_inventory_product(
            "Штани Broken Planet", ["M"], ["GRAY"]
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.set_product_transport_cost(product["id"], "SEA", "400", 108)
        receipt = business_db.receive_inventory_batch(variant["id"], "SEA", 1, 108)
        order = business_db.record_order(
            ttn="20451424252999",
            sale_type_label="Наложка",
            carrier="Nova Poshta",
            product_name="Штани Broken Planet сірі M",
        )
        business_db.reserve_order_by_ttn(order["ttn"], 108, "Owner")

        with self.assertRaisesRegex(ValueError, "зарезервована"):
            business_db.undo_inventory_movement(receipt["movement_id"], 108)

    def test_telegram_undo_button_restores_last_receipt(self):
        product = business_db.create_inventory_product(
            "Штани Suvene", ["M"], ["BLACK"]
        )
        variant = business_db.list_product_variants(product["id"])[0]
        business_db.set_product_transport_cost(product["id"], "SEA", "400", 108)
        receipt = business_db.receive_inventory_batch(variant["id"], "SEA", 5, 108)
        bot = FakeBot()
        user = SimpleNamespace(id=108, full_name="Owner", username="owner")
        source_message = SimpleNamespace(
            message_id=1, chat=SimpleNamespace(id=108), text="Склад"
        )

        inventory.handle_inventory_callback(
            bot,
            SimpleNamespace(
                id="undo-ask",
                data=f"inv:undoask:x:{receipt['movement_id']}",
                from_user=user,
                message=source_message,
            ),
        )
        inventory.handle_inventory_callback(
            bot,
            SimpleNamespace(
                id="undo-ok",
                data=f"inv:undook:x:{receipt['movement_id']}",
                from_user=user,
                message=source_message,
            ),
        )

        self.assertEqual(business_db.get_inventory_variant(variant["id"])["on_hand"], 0)
        self.assertIn("скасовано", bot.edited[-1]["text"])


if __name__ == "__main__":
    unittest.main()
