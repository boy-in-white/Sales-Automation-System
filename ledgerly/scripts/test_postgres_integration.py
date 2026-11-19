#!/usr/bin/env python3
"""Destructive end-to-end check for a dedicated PostgreSQL test database."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import business_db  # noqa: E402
import tracking_db  # noqa: E402
from config import DATABASE_URL  # noqa: E402
from db_runtime import close_pools, database_backend  # noqa: E402


def _reset_test_schemas() -> None:
    import psycopg
    from psycopg import sql

    database_name = unquote(urlsplit(DATABASE_URL).path.lstrip("/"))
    if "test" not in database_name.casefold():
        raise RuntimeError(
            "Refusing to delete schemas: the PostgreSQL database name must contain 'test'"
        )
    close_pools()
    with psycopg.connect(DATABASE_URL) as connection:
        for schema in ("analytics", "business", "bot"):
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


def _manual_order_integration(product, variant):
    import callbacks
    import order_entry
    import repliq_inbox

    class Bot:
        def __init__(self):
            self.sent = []
        def send_message(self, chat_id, text, **kwargs):
            message = SimpleNamespace(message_id=len(self.sent) + 1, text=text,
                chat=SimpleNamespace(id=chat_id), reply_markup=kwargs.get('reply_markup'))
            self.sent.append(message)
            return message
        def answer_callback_query(self, *args, **kwargs):
            pass
        def edit_message_text(self, *args, **kwargs):
            pass

    bot, user = Bot(), SimpleNamespace(id=1, full_name='Integration owner')
    def press(action, value='x'):
        payload = business_db.get_inventory_wizard(1)['payload']
        call = SimpleNamespace(id='test', from_user=user,
            message=SimpleNamespace(chat=SimpleNamespace(id=1, type='private'), message_id=1),
            data='new:%s:%s:%s:%s' % (payload['token'], payload['revision'], action, value))
        callbacks.handle_callback(bot, call)
        return call
    with patch.object(callbacks, 'is_admin', return_value=True), patch.object(callbacks, 'is_employee', return_value=True), \
         patch.object(repliq_inbox, '_RECEIVER', None), patch('incoming_group_listener.process_manual_ttn', return_value=(True, {}, False, 'ok')):
        order_entry.start_order(bot, 1, 1)
        press('kind', 'olx'); press('product', product['id']); press('color', 'BLACK'); press('size', variant['id']); press('qty', '2')
        for value in ('2100,50', '20451424252957'):
            order_entry.handle_text(bot, SimpleNamespace(chat=SimpleNamespace(id=1), from_user=user, text=value))
        assert business_db.get_inventory_wizard(1)['step'] == 'ORDER_REVIEW'
        token = business_db.get_inventory_wizard(1)['payload']['token']
        saved_call = press('save')
        callbacks.handle_callback(bot, saved_call)
        order = business_db.get_order_by_ttn('20451424252957')
        assert order['product_id'] == product['id'] and order['quantity'] == 2 and order['sale_price_cents'] == 210050
        assert business_db.get_inventory_variant(variant['id'])['reserved'] == 2
        assert business_db.get_submission(token)['status'] == 'CONFIRMED'
        business_db.mark_order_shipped_by_ttn(order['ttn'], 1)
        result = business_db.mark_order_in_1c(order['id'], 1)
        assert result['deducted_quantity'] == 2 and result['purchase_cost_cents'] == 100000
        assert business_db.mark_order_in_1c(order['id'], 1)['already_recorded']
        assert business_db.get_inventory_variant(variant['id'])['on_hand'] == 2
    print('PostgreSQL manual order: buttons, atomic save, reservation, shipping, FIFO and duplicate protection: OK')


def main() -> None:
    if database_backend(DATABASE_URL) != "postgresql":
        raise RuntimeError("Set DATABASE_URL to a dedicated PostgreSQL test database")

    _reset_test_schemas()
    tracking_db.init_db()
    business_db.init_business_db()

    product_a = business_db.resolve_product("штани SUVENE")
    product_b = business_db.resolve_product("suvene Штани")
    assert product_a and product_b and product_a["id"] == product_b["id"]
    assert product_a["canonical_name"] == "Штани Suvene"
    configured = business_db.create_inventory_product(
        "Штани Suvene",
        ["M"],
        ["BLACK"],
    )
    variant = business_db.list_product_variants(configured["id"])[0]
    business_db.set_product_transport_cost(
        configured["id"], "SEA", "500", 1, "Test owner"
    )
    business_db.receive_inventory_batch(variant["id"], "SEA", 5, 1, "Test owner")

    order = business_db.record_order(
        ttn="20451424252955",
        sale_type_label="ОЛХ",
        carrier="Nova Poshta",
        product_name="SUVENE штани чорні M",
        sale_price="1200.50",
        delivery_cost="100",
        platform_fee="50",
        olx_order_id="TEST-OLX-1",
        source_user_id=1,
        source_user_name="Test owner",
    )
    shipped = business_db.mark_order_shipped_by_ttn(order["ttn"], 1, "Test owner")
    assert shipped and shipped["status"] == "SHIPPED" and shipped["shipped_at"]
    delivered = business_db.update_order_status_by_ttn(
        order["ttn"], "DELIVERED", actor_user_id=1, actor_name="Test owner"
    )
    assert delivered and delivered["sold_at"]
    stock_result = business_db.mark_order_in_1c(order["id"], 1, "Test owner")
    assert stock_result and stock_result["inventory_status"] == "written_off"
    assert stock_result["balance_after"] == 4
    assert stock_result["purchase_cost_cents"] == 50000
    assert stock_result["profit_cents"] == 55050

    refused = business_db.record_order(
        ttn="20451424252956",
        sale_type_label="Наложка",
        carrier="Nova Poshta",
        product_name="Куртка test",
        sale_price="900",
    )
    refused = business_db.update_order_status_by_ttn(
        refused["ttn"], "REFUSED", actor_user_id=1, reason="Клієнт відмовився"
    )
    assert refused and refused["refused_at"]

    live_stats = business_db.get_sales_stats("all")
    assert live_stats["sales_count"] == 1
    assert live_stats["turnover_cents"] == 120050
    assert live_stats["profit_cents"] == 55050
    assert live_stats["refusals_count"] == 1

    test_month = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%Y-%m")
    business_db.upsert_monthly_turnover(test_month, "1200.50", orders_count=1)
    turnover = business_db.get_monthly_turnover(test_month)
    assert turnover and turnover["turnover_cents"] == 120050

    tracking_db.upsert_shipment(
        order["ttn"],
        description="Штани Suvene",
        source_user_id=1,
    )
    tracking_db.update_shipment(order["ttn"], next_check_at=0)
    claimed = tracking_db.claim_due_shipments(limit=10, lease_seconds=60)
    assert [row["ttn"] for row in claimed] == [order["ttn"]]
    assert tracking_db.claim_group_message(-1001, 7, 99)
    tracking_db.complete_group_message(-1001, 7, 99)
    assert tracking_db.seen_group_message(-1001, 7, 99)

    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        schemas = {
            row[0]
            for row in connection.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name IN ('business', 'bot', 'analytics')"
            )
        }
        assert schemas == {"business", "bot", "analytics"}
        monthly = connection.execute(
            "SELECT revenue_cents, profit_cents FROM analytics.v_monthly_sales "
            "WHERE month = %s",
            (test_month,),
        ).fetchone()
        assert monthly and monthly[0] == 120050 and monthly[1] == 55050
        refusal_count = connection.execute(
            "SELECT COUNT(*) FROM analytics.v_refusals"
        ).fetchone()[0]
        assert refusal_count == 1
        stock = connection.execute(
            "SELECT available FROM analytics.v_inventory_stock "
            "WHERE variant_id = %s",
            (variant["id"],),
        ).fetchone()
        assert stock and stock[0] == 4
        movement = connection.execute(
            "SELECT quantity_delta FROM analytics.v_inventory_movements "
            "WHERE order_id = %s",
            (order["id"],),
        ).fetchone()
        assert movement and movement[0] == -1
        batch = connection.execute(
            "SELECT transport_mode, remaining_quantity "
            "FROM analytics.v_inventory_batches WHERE variant_id = %s",
            (variant["id"],),
        ).fetchone()
        assert batch and batch[0] == "SEA" and batch[1] == 4

    _manual_order_integration(configured, variant)
    close_pools()
    print("PostgreSQL integration: OK")
    print("Schemas, inventory, sale, refusal, turnover, tracking and views: OK")


if __name__ == "__main__":
    main()
