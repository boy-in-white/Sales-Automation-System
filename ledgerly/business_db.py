"""Business data in PostgreSQL, with SQLite retained for tests/import only.

Money is stored as integer kopiykas to avoid floating-point errors.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from config import BUSINESS_DATABASE_URL, DATABASE_PATH, TIMEZONE
from db_runtime import (
    Session,
    bootstrap_postgres_schemas,
    database_backend,
    transaction,
)

SALE_TYPES = {"ОЛХ": "OLX", "Наложка": "COD", "ФОП": "FOP", "Офлайн": "OFFLINE"}
SALE_TYPE_LABELS = {value: key for key, value in SALE_TYPES.items()}
SIZE_CODES = ("XS", "S", "M", "L", "XL")
COLOR_LABELS = {
    "BLACK": "Чорний",
    "WHITE": "Білий",
    "GRAY": "Сірий",
}
TRANSPORT_LABELS = {
    "SEA": "Море",
    "AIR": "Літак",
}
COLOR_WORDS = {
    "black": "BLACK",
    "чорний": "BLACK",
    "чорна": "BLACK",
    "чорне": "BLACK",
    "чорні": "BLACK",
    "чорного": "BLACK",
    "white": "WHITE",
    "білий": "WHITE",
    "біла": "WHITE",
    "біле": "WHITE",
    "білі": "WHITE",
    "білого": "WHITE",
    "gray": "GRAY",
    "grey": "GRAY",
    "сірий": "GRAY",
    "сіра": "GRAY",
    "сіре": "GRAY",
    "сірі": "GRAY",
    "сірого": "GRAY",
}
ORDER_STATUSES = {
    "NEW",
    "CONFIRMED",
    "SHIPPED",
    "DELIVERED",
    "REFUSED",
    "RETURNED",
    "RESHIP_REQUIRED",
    "CANCELLED",
    "CLOSED",
}
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _transaction():
    return transaction(
        BUSINESS_DATABASE_URL,
        schemas=("business", "analytics", "public"),
        sqlite_default_path=DATABASE_PATH,
    )


def _schema_statements(backend: str) -> list[str]:
    timestamp_type = "TIMESTAMPTZ" if backend == "postgresql" else "TEXT"
    integer_type = "BIGINT" if backend == "postgresql" else "INTEGER"
    view_prefix = (
        "CREATE OR REPLACE VIEW"
        if backend == "postgresql"
        else "CREATE VIEW IF NOT EXISTS"
    )
    analytics_prefix = "analytics." if backend == "postgresql" else ""
    statements = [
        f"""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            normalized_key TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL DEFAULT '',
            brand TEXT NOT NULL DEFAULT '',
            active SMALLINT NOT NULL DEFAULT 1,
            created_at {timestamp_type} NOT NULL,
            updated_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS product_aliases (
            alias_key TEXT PRIMARY KEY,
            raw_alias TEXT NOT NULL,
            product_id TEXT NOT NULL REFERENCES products(id),
            created_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS product_variants (
            id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL REFERENCES products(id),
            color_code TEXT NOT NULL CHECK (color_code IN ('BLACK', 'WHITE', 'GRAY')),
            size_code TEXT NOT NULL CHECK (size_code IN ('XS', 'S', 'M', 'L', 'XL')),
            active SMALLINT NOT NULL DEFAULT 1,
            created_at {timestamp_type} NOT NULL,
            updated_at {timestamp_type} NOT NULL,
            UNIQUE(product_id, color_code, size_code)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS inventory_balances (
            variant_id TEXT PRIMARY KEY REFERENCES product_variants(id),
            on_hand INTEGER NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
            reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
            updated_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS product_cost_profiles (
            product_id TEXT NOT NULL REFERENCES products(id),
            transport_mode TEXT NOT NULL CHECK (transport_mode IN ('SEA', 'AIR')),
            unit_cost_cents {integer_type} NOT NULL CHECK (unit_cost_cents > 0),
            updated_by {integer_type} NOT NULL DEFAULT 0,
            updated_by_name TEXT NOT NULL DEFAULT '',
            created_at {timestamp_type} NOT NULL,
            updated_at {timestamp_type} NOT NULL,
            PRIMARY KEY (product_id, transport_mode)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS inventory_batches (
            id TEXT PRIMARY KEY,
            variant_id TEXT NOT NULL REFERENCES product_variants(id),
            transport_mode TEXT NOT NULL CHECK (transport_mode IN ('SEA', 'AIR')),
            unit_cost_cents {integer_type} NOT NULL CHECK (unit_cost_cents > 0),
            received_quantity INTEGER NOT NULL CHECK (received_quantity > 0),
            remaining_quantity INTEGER NOT NULL CHECK (remaining_quantity >= 0),
            actor_user_id {integer_type} NOT NULL DEFAULT 0,
            actor_name TEXT NOT NULL DEFAULT '',
            received_at {timestamp_type} NOT NULL,
            created_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS inventory_movements (
            id TEXT PRIMARY KEY,
            variant_id TEXT NOT NULL REFERENCES product_variants(id),
            order_id TEXT NOT NULL DEFAULT '',
            batch_id TEXT NOT NULL DEFAULT '',
            quantity_delta INTEGER NOT NULL,
            balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
            movement_type TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            actor_user_id {integer_type} NOT NULL DEFAULT 0,
            actor_name TEXT NOT NULL DEFAULT '',
            created_at {timestamp_type} NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS inventory_movement_batches (
            movement_id TEXT NOT NULL REFERENCES inventory_movements(id),
            batch_id TEXT NOT NULL REFERENCES inventory_batches(id),
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (movement_id, batch_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS inventory_movement_reversals (
            original_movement_id TEXT PRIMARY KEY REFERENCES inventory_movements(id),
            reversal_movement_id TEXT NOT NULL UNIQUE REFERENCES inventory_movements(id),
            actor_user_id {integer_type} NOT NULL DEFAULT 0,
            actor_name TEXT NOT NULL DEFAULT '',
            created_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS inventory_wizards (
            user_id {integer_type} PRIMARY KEY,
            chat_id {integer_type} NOT NULL,
            step TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{{}}',
            created_at {timestamp_type} NOT NULL,
            updated_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS order_submissions (
            id TEXT PRIMARY KEY,
            raw_text TEXT NOT NULL,
            parsed_json TEXT NOT NULL,
            ttns_json TEXT NOT NULL,
            source_user_id {integer_type} NOT NULL DEFAULT 0,
            source_user_name TEXT NOT NULL DEFAULT '',
            telegram_chat_id {integer_type} NOT NULL DEFAULT 0,
            telegram_message_id {integer_type} NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'PENDING',
            reviewed_by {integer_type} NOT NULL DEFAULT 0,
            reviewed_at {timestamp_type},
            created_at {timestamp_type} NOT NULL,
            updated_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            ttn TEXT UNIQUE,
            olx_order_id TEXT NOT NULL DEFAULT '',
            sale_type TEXT NOT NULL,
            carrier TEXT NOT NULL DEFAULT '',
            product_id TEXT REFERENCES products(id),
            raw_product_name TEXT NOT NULL DEFAULT '',
            quantity INTEGER NOT NULL DEFAULT 1,
            sale_price_cents {integer_type} NOT NULL DEFAULT 0,
            purchase_cost_cents {integer_type} NOT NULL DEFAULT 0,
            delivery_cost_cents {integer_type} NOT NULL DEFAULT 0,
            platform_fee_cents {integer_type} NOT NULL DEFAULT 0,
            other_cost_cents {integer_type} NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'UAH',
            status TEXT NOT NULL DEFAULT 'NEW',
            shipped_at {timestamp_type},
            sold_at {timestamp_type},
            refused_at {timestamp_type},
            returned_at {timestamp_type},
            return_reason TEXT NOT NULL DEFAULT '',
            onec_recorded_at {timestamp_type},
            onec_recorded_by {integer_type} NOT NULL DEFAULT 0,
            cost_source TEXT NOT NULL DEFAULT '',
            cost_calculated_at {timestamp_type},
            source_user_id {integer_type} NOT NULL DEFAULT 0,
            source_user_name TEXT NOT NULL DEFAULT '',
            created_at {timestamp_type} NOT NULL,
            updated_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS inventory_batch_allocations (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES inventory_batches(id),
            order_id TEXT NOT NULL REFERENCES orders(id),
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            unit_cost_cents {integer_type} NOT NULL CHECK (unit_cost_cents > 0),
            total_cost_cents {integer_type} NOT NULL CHECK (total_cost_cents > 0),
            created_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS inventory_reservations (
            order_id TEXT PRIMARY KEY REFERENCES orders(id),
            variant_id TEXT NOT NULL REFERENCES product_variants(id),
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CONSUMED', 'RELEASED')),
            actor_user_id {integer_type} NOT NULL DEFAULT 0,
            actor_name TEXT NOT NULL DEFAULT '',
            reserved_at {timestamp_type} NOT NULL,
            released_at {timestamp_type},
            updated_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS order_events (
            id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL REFERENCES orders(id),
            event_type TEXT NOT NULL,
            actor_user_id {integer_type} NOT NULL DEFAULT 0,
            actor_name TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{{}}',
            created_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS order_notifications (
            order_id TEXT NOT NULL REFERENCES orders(id),
            notification_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'CLAIMED',
            telegram_message_id {integer_type} NOT NULL DEFAULT 0,
            claimed_at {timestamp_type} NOT NULL,
            updated_at {timestamp_type} NOT NULL,
            PRIMARY KEY (order_id, notification_type)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {analytics_prefix}monthly_turnover (
            month TEXT PRIMARY KEY,
            turnover_cents {integer_type} NOT NULL,
            currency TEXT NOT NULL DEFAULT 'UAH',
            orders_count INTEGER NOT NULL DEFAULT 0,
            calculated_at {timestamp_type} NOT NULL,
            source TEXT NOT NULL DEFAULT 'external-analytics',
            note TEXT NOT NULL DEFAULT '',
            updated_at {timestamp_type} NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {analytics_prefix}monthly_analytics_resets (
            month TEXT PRIMARY KEY,
            cleared_at {timestamp_type} NOT NULL,
            cleared_by {integer_type} NOT NULL DEFAULT 0,
            cleared_by_name TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_orders_sold_at ON orders(sold_at)",
        "CREATE INDEX IF NOT EXISTS idx_orders_refused_at ON orders(refused_at)",
        "CREATE INDEX IF NOT EXISTS idx_orders_product ON orders(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
        "CREATE INDEX IF NOT EXISTS idx_orders_sale_type ON orders(sale_type)",
        "CREATE INDEX IF NOT EXISTS idx_events_order_time ON order_events(order_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_submissions_status ON order_submissions(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_variants_product ON product_variants(product_id, active)",
        (
            "CREATE INDEX IF NOT EXISTS idx_inventory_batches_fifo "
            "ON inventory_batches(variant_id, remaining_quantity, received_at)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_batch_allocations_order "
            "ON inventory_batch_allocations(order_id)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_inventory_movements_variant_time "
            "ON inventory_movements(variant_id, created_at)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_inventory_movement_batches_batch "
            "ON inventory_movement_batches(batch_id)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_inventory_reservations_status "
            "ON inventory_reservations(status, variant_id)"
        ),
        f"""
        {view_prefix} {analytics_prefix}v_order_financials AS
        SELECT o.id, o.ttn, o.olx_order_id, o.sale_type, o.carrier,
               COALESCE(p.canonical_name, o.raw_product_name) AS product_name,
               o.quantity, o.sale_price_cents,
               (o.purchase_cost_cents + o.delivery_cost_cents + o.platform_fee_cents + o.other_cost_cents) AS total_cost_cents,
               CASE WHEN o.cost_source IN ('FIFO_BATCH', 'MANUAL')
                    THEN (o.sale_price_cents - o.purchase_cost_cents - o.delivery_cost_cents - o.platform_fee_cents - o.other_cost_cents)
                    ELSE NULL END AS profit_cents,
               o.status, o.sold_at, o.refused_at, o.onec_recorded_at,
               o.purchase_cost_cents, o.delivery_cost_cents,
               o.platform_fee_cents, o.other_cost_cents, o.cost_source
        FROM orders o LEFT JOIN products p ON p.id = o.product_id
        """,
        f"""
        {view_prefix} {analytics_prefix}v_refusals AS
        SELECT o.id, o.ttn, COALESCE(p.canonical_name, o.raw_product_name) AS product_name,
               o.sale_type, o.refused_at, o.returned_at, o.return_reason
        FROM orders o LEFT JOIN products p ON p.id = o.product_id
        WHERE o.refused_at IS NOT NULL
        """,
        f"""
        {view_prefix} {analytics_prefix}v_inventory_commitments AS
        SELECT r.order_id,
               o.ttn,
               r.variant_id,
               r.quantity,
               CASE WHEN o.status = 'CONFIRMED'
                    THEN 'AWAITING_SHIPMENT' ELSE 'IN_TRANSIT' END AS stage,
               r.reserved_at,
               r.updated_at
        FROM inventory_reservations r
        JOIN orders o ON o.id = r.order_id
        WHERE r.status = 'ACTIVE'
        UNION ALL
        SELECT o.id AS order_id,
               o.ttn,
               i.variant_id,
               i.reserved_quantity AS quantity,
               CASE WHEN o.status = 'CONFIRMED'
                    THEN 'AWAITING_SHIPMENT' ELSE 'IN_TRANSIT' END AS stage,
               o.created_at AS reserved_at,
               o.updated_at
        FROM repliq_parcel_items i
        JOIN orders o ON o.ttn = i.ttn
        WHERE i.state = 'RESERVED' AND i.reserved_quantity > 0
        """,
        f"""
        {view_prefix} {analytics_prefix}v_inventory_stock AS
        SELECT p.id AS product_id,
               p.canonical_name AS product_name,
               v.id AS variant_id,
               v.color_code,
               CASE v.color_code
                   WHEN 'BLACK' THEN 'Чорний'
                   WHEN 'WHITE' THEN 'Білий'
                   WHEN 'GRAY' THEN 'Сірий'
               END AS color_name,
               v.size_code,
               COALESCE(b.on_hand, 0) AS on_hand,
               COALESCE(b.reserved, 0) AS reserved,
               COALESCE(b.on_hand, 0) - COALESCE(b.reserved, 0) AS available,
               b.updated_at,
               COALESCE(c.in_transit, 0) AS in_transit,
               COALESCE(c.awaiting_shipment, 0) AS awaiting_shipment
        FROM products p
        JOIN product_variants v ON v.product_id = p.id AND v.active = 1
        LEFT JOIN inventory_balances b ON b.variant_id = v.id
        LEFT JOIN (
            SELECT variant_id,
                   CAST(SUM(CASE WHEN stage = 'IN_TRANSIT' THEN quantity ELSE 0 END) AS INTEGER)
                       AS in_transit,
                   CAST(SUM(CASE WHEN stage = 'AWAITING_SHIPMENT' THEN quantity ELSE 0 END) AS INTEGER)
                       AS awaiting_shipment
            FROM {analytics_prefix}v_inventory_commitments
            GROUP BY variant_id
        ) c ON c.variant_id = v.id
        WHERE p.active = 1
        """,
        f"""
        {view_prefix} {analytics_prefix}v_inventory_movements AS
        SELECT m.id,
               m.order_id,
               p.id AS product_id,
               p.canonical_name AS product_name,
               v.id AS variant_id,
               v.color_code,
               CASE v.color_code
                   WHEN 'BLACK' THEN 'Чорний'
                   WHEN 'WHITE' THEN 'Білий'
                   WHEN 'GRAY' THEN 'Сірий'
               END AS color_name,
               v.size_code,
               m.quantity_delta,
               m.balance_after,
               m.movement_type,
               m.note,
               m.actor_user_id,
               m.actor_name,
               m.created_at,
               m.batch_id
        FROM inventory_movements m
        JOIN product_variants v ON v.id = m.variant_id
        JOIN products p ON p.id = v.product_id
        """,
        f"""
        {view_prefix} {analytics_prefix}v_inventory_in_transit AS
        SELECT c.order_id,
               c.ttn,
               p.id AS product_id,
               p.canonical_name AS product_name,
               v.id AS variant_id,
               v.color_code,
               CASE v.color_code
                   WHEN 'BLACK' THEN 'Чорний'
                   WHEN 'WHITE' THEN 'Білий'
                   WHEN 'GRAY' THEN 'Сірий'
               END AS color_name,
               v.size_code,
               c.quantity,
               o.status AS order_status,
               c.reserved_at,
               c.updated_at
        FROM {analytics_prefix}v_inventory_commitments c
        JOIN orders o ON o.id = c.order_id
        JOIN product_variants v ON v.id = c.variant_id
        JOIN products p ON p.id = v.product_id
        WHERE c.stage = 'IN_TRANSIT'
        """,
        f"""
        {view_prefix} {analytics_prefix}v_inventory_awaiting_shipment AS
        SELECT c.order_id,
               c.ttn,
               p.id AS product_id,
               p.canonical_name AS product_name,
               v.id AS variant_id,
               v.color_code,
               CASE v.color_code
                   WHEN 'BLACK' THEN 'Чорний'
                   WHEN 'WHITE' THEN 'Білий'
                   WHEN 'GRAY' THEN 'Сірий'
               END AS color_name,
               v.size_code,
               c.quantity,
               o.status AS order_status,
               c.reserved_at,
               c.updated_at
        FROM {analytics_prefix}v_inventory_commitments c
        JOIN orders o ON o.id = c.order_id
        JOIN product_variants v ON v.id = c.variant_id
        JOIN products p ON p.id = v.product_id
        WHERE c.stage = 'AWAITING_SHIPMENT'
        """,
        f"""
        {view_prefix} {analytics_prefix}v_inventory_batches AS
        SELECT b.id AS batch_id,
               p.id AS product_id,
               p.canonical_name AS product_name,
               v.id AS variant_id,
               v.color_code,
               CASE v.color_code
                   WHEN 'BLACK' THEN 'Чорний'
                   WHEN 'WHITE' THEN 'Білий'
                   WHEN 'GRAY' THEN 'Сірий'
               END AS color_name,
               v.size_code,
               b.transport_mode,
               CASE b.transport_mode WHEN 'SEA' THEN 'Море' WHEN 'AIR' THEN 'Літак' END
                   AS transport_name,
               b.unit_cost_cents,
               b.received_quantity,
               b.remaining_quantity,
               b.received_at
        FROM inventory_batches b
        JOIN product_variants v ON v.id = b.variant_id
        JOIN products p ON p.id = v.product_id
        """,
    ]
    month_expression = (
        "TO_CHAR(sold_at AT TIME ZONE 'Europe/Kyiv', 'YYYY-MM')"
        if backend == "postgresql"
        else "substr(sold_at, 1, 7)"
    )
    reset_table = (
        "analytics.monthly_analytics_resets"
        if backend == "postgresql"
        else "monthly_analytics_resets"
    )
    statements.append(
        f"""
        {view_prefix} {analytics_prefix}v_monthly_sales AS
        SELECT {month_expression} AS month,
               COUNT(*) AS sales_count,
               SUM(quantity) AS items_count,
               SUM(sale_price_cents) AS revenue_cents,
               SUM(CASE WHEN cost_source IN ('FIFO_BATCH', 'MANUAL')
                             AND sale_price_cents > 0
                        THEN purchase_cost_cents + delivery_cost_cents + platform_fee_cents + other_cost_cents
                        ELSE 0 END) AS expenses_cents,
               SUM(CASE WHEN cost_source IN ('FIFO_BATCH', 'MANUAL')
                             AND sale_price_cents > 0
                        THEN sale_price_cents - purchase_cost_cents - delivery_cost_cents - platform_fee_cents - other_cost_cents
                        ELSE 0 END) AS profit_cents,
               SUM(CASE WHEN cost_source NOT IN ('FIFO_BATCH', 'MANUAL')
                             OR sale_price_cents <= 0 THEN 1 ELSE 0 END)
                   AS cost_pending_count
        FROM orders
        WHERE sold_at IS NOT NULL AND status NOT IN ('REFUSED', 'RETURNED', 'CANCELLED')
          AND NOT EXISTS (
              SELECT 1 FROM {reset_table} r
              WHERE r.month = {month_expression} AND sold_at < r.cleared_at
          )
        GROUP BY {month_expression}
        """
    )
    return statements


def init_business_db() -> None:
    bootstrap_postgres_schemas(
        BUSINESS_DATABASE_URL,
        ("business", "analytics"),
    )
    with _LOCK, _transaction() as session:
        backend = database_backend(BUSINESS_DATABASE_URL)
        statements = _schema_statements(backend)
        from parcel_manifest import schema_statement
        statements.append(schema_statement())
        regular_statements = [
            item for item in statements if " VIEW " not in item.upper()
        ]
        view_statements = [item for item in statements if " VIEW " in item.upper()]
        for statement in regular_statements:
            session.execute(statement)
        if backend == "postgresql":
            session.execute(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipped_at TIMESTAMPTZ"
            )
            session.execute(
                "ALTER TABLE inventory_movements "
                "ADD COLUMN IF NOT EXISTS order_id TEXT NOT NULL DEFAULT ''"
            )
            session.execute(
                "ALTER TABLE inventory_movements "
                "ADD COLUMN IF NOT EXISTS batch_id TEXT NOT NULL DEFAULT ''"
            )
            session.execute(
                "ALTER TABLE orders "
                "ADD COLUMN IF NOT EXISTS cost_source TEXT NOT NULL DEFAULT ''"
            )
            session.execute(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS cost_calculated_at TIMESTAMPTZ"
            )
        else:
            order_columns = {
                str(row["name"]) for row in session.all("PRAGMA table_info(orders)")
            }
            if "shipped_at" not in order_columns:
                session.execute("ALTER TABLE orders ADD COLUMN shipped_at TEXT")
            movement_columns = {
                str(row["name"])
                for row in session.all("PRAGMA table_info(inventory_movements)")
            }
            if "order_id" not in movement_columns:
                session.execute(
                    "ALTER TABLE inventory_movements "
                    "ADD COLUMN order_id TEXT NOT NULL DEFAULT ''"
                )
            if "batch_id" not in movement_columns:
                session.execute(
                    "ALTER TABLE inventory_movements "
                    "ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''"
                )
            if "cost_source" not in order_columns:
                session.execute(
                    "ALTER TABLE orders ADD COLUMN cost_source TEXT NOT NULL DEFAULT ''"
                )
            if "cost_calculated_at" not in order_columns:
                session.execute("ALTER TABLE orders ADD COLUMN cost_calculated_at TEXT")
        session.execute(
            "UPDATE orders SET cost_source = 'MANUAL', cost_calculated_at = COALESCE(cost_calculated_at, updated_at) "
            "WHERE purchase_cost_cents > 0 AND cost_source = ''"
        )
        for statement in view_statements:
            session.execute(statement)
        session.execute(
            """
            INSERT INTO schema_migrations(component, version, applied_at)
            VALUES ('business', 8, ?)
            ON CONFLICT(component) DO UPDATE SET
                version = excluded.version,
                applied_at = excluded.applied_at
            """,
            (_now(),),
        )


def normalize_product_key(name: str) -> str:
    text = unicodedata.normalize("NFKC", name or "").casefold()
    text = text.replace("’", "'").replace("`", "'")
    tokens = re.findall(r"[a-zа-яіїєґ0-9]+", text, re.I)
    return " ".join(sorted(tokens))


def canonicalize_product_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name or "").strip()
    tokens = re.findall(r"[A-Za-zА-ЯІЇЄҐа-яіїєґ0-9'’-]+", text)
    cyrillic = [
        token.casefold() for token in tokens if re.search(r"[А-ЯІЇЄҐа-яіїєґ]", token)
    ]
    other = [token for token in tokens if not re.search(r"[А-ЯІЇЄҐа-яіїєґ]", token)]
    parts: list[str] = []
    if cyrillic:
        phrase = " ".join(cyrillic)
        parts.append(phrase[:1].upper() + phrase[1:])
    parts.extend(token.title() if token.isalpha() else token.upper() for token in other)
    return " ".join(parts)[:200] or "Невідомий товар"


def money_to_cents(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        amount = Decimal(str(value).replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return 0
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_money(cents: int | None) -> str:
    return f"{Decimal(int(cents or 0)) / 100:.2f}"


def resolve_product(name: str, create: bool = True) -> dict[str, Any] | None:
    key = normalize_product_key(name)
    if not key:
        return None
    with _LOCK, _transaction() as session:
        row = session.one(
            """
            SELECT p.* FROM product_aliases a
            JOIN products p ON p.id = a.product_id
            WHERE a.alias_key = ?
            """,
            (key,),
        )
        if row:
            return row
        row = session.one("SELECT * FROM products WHERE normalized_key = ?", (key,))
        if row:
            session.execute(
                "INSERT INTO product_aliases(alias_key, raw_alias, product_id, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(alias_key) DO NOTHING",
                (key, name.strip(), row["id"], _now()),
            )
            return row
        if not create:
            return None
        product_id = uuid.uuid4().hex[:20]
        canonical = canonicalize_product_name(name)
        now = _now()
        session.execute(
            """
            INSERT INTO products(id, canonical_name, normalized_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(normalized_key) DO NOTHING
            """,
            (product_id, canonical, key, now, now),
        )
        product = session.one("SELECT * FROM products WHERE normalized_key = ?", (key,))
        product_id = product["id"]
        session.execute(
            "INSERT INTO product_aliases(alias_key, raw_alias, product_id, created_at) VALUES (?, ?, ?, ?)",
            (key, name.strip(), product_id, now),
        )
        return product


def list_products(limit: int = 100) -> list[dict[str, Any]]:
    with _LOCK, _transaction() as session:
        return session.all(
            "SELECT * FROM products WHERE active = 1 ORDER BY canonical_name LIMIT ?",
            (max(1, min(limit, 500)),),
        )


def rename_product(product_id: str, new_name: str) -> dict[str, Any] | None:
    key = normalize_product_key(new_name)
    if not key:
        return None
    canonical = canonicalize_product_name(new_name)
    with _LOCK, _transaction() as session:
        existing = session.one("SELECT * FROM products WHERE id = ?", (product_id,))
        if not existing:
            return None
        conflict = session.one(
            "SELECT id FROM products WHERE normalized_key = ? AND id <> ?",
            (key, product_id),
        )
        if conflict:
            return None
        session.execute(
            "UPDATE products SET canonical_name = ?, normalized_key = ?, updated_at = ? WHERE id = ?",
            (canonical, key, _now(), product_id),
        )
        session.execute(
            "INSERT INTO product_aliases(alias_key, raw_alias, product_id, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(alias_key) DO UPDATE SET product_id = ?",
            (key, new_name.strip(), product_id, _now(), product_id),
        )
        return session.one("SELECT * FROM products WHERE id = ?", (product_id,))


def add_product_alias(product_id: str, alias: str) -> bool:
    key = normalize_product_key(alias)
    if not key:
        return False
    with _LOCK, _transaction() as session:
        if not session.one("SELECT id FROM products WHERE id = ?", (product_id,)):
            return False
        session.execute(
            "INSERT INTO product_aliases(alias_key, raw_alias, product_id, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(alias_key) DO UPDATE SET product_id = ?",
            (key, alias.strip(), product_id, _now(), product_id),
        )
    return True


def _validated_sizes(values: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    selected = {str(value).strip().upper() for value in values}
    result = [size for size in SIZE_CODES if size in selected]
    if not result or selected.difference(SIZE_CODES):
        raise ValueError("Потрібно вибрати хоча б один правильний розмір")
    return result


def _validated_colors(values: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    selected = {str(value).strip().upper() for value in values}
    result = [color for color in COLOR_LABELS if color in selected]
    if not result or selected.difference(COLOR_LABELS):
        raise ValueError("Потрібно вибрати хоча б один правильний колір")
    return result


def configure_product_variants(
    product_id: str,
    sizes: list[str] | tuple[str, ...] | set[str],
    colors: list[str] | tuple[str, ...] | set[str],
) -> dict[str, Any]:
    """Set the allowed size/color matrix without losing stock history."""

    selected_sizes = _validated_sizes(sizes)
    selected_colors = _validated_colors(colors)
    allowed = {(color, size) for color in selected_colors for size in selected_sizes}
    now = _now()
    with _LOCK, _transaction() as session:
        product = session.one(
            "SELECT * FROM products WHERE id = ? AND active = 1", (product_id,)
        )
        if not product:
            raise ValueError("Товар не знайдено")

        variants = session.all(
            """
            SELECT v.*, COALESCE(b.on_hand, 0) AS on_hand,
                   COALESCE(b.reserved, 0) AS reserved
            FROM product_variants v
            LEFT JOIN inventory_balances b ON b.variant_id = v.id
            WHERE v.product_id = ?
            """,
            (product_id,),
        )
        blocked = [
            f"{COLOR_LABELS.get(row['color_code'], row['color_code'])} {row['size_code']}"
            for row in variants
            if (row["color_code"], row["size_code"]) not in allowed
            and (int(row.get("on_hand") or 0) > 0 or int(row.get("reserved") or 0) > 0)
        ]
        if blocked:
            raise ValueError("Спочатку обнули залишок для: " + ", ".join(blocked[:8]))

        session.execute(
            "UPDATE product_variants SET active = 0, updated_at = ? WHERE product_id = ?",
            (now, product_id),
        )
        for color in selected_colors:
            for size in selected_sizes:
                variant_id = uuid.uuid4().hex[:20]
                session.execute(
                    """
                    INSERT INTO product_variants(
                        id, product_id, color_code, size_code, active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(product_id, color_code, size_code) DO UPDATE SET
                        active = 1,
                        updated_at = excluded.updated_at
                    """,
                    (variant_id, product_id, color, size, now, now),
                )
                variant = session.one(
                    "SELECT id FROM product_variants "
                    "WHERE product_id = ? AND color_code = ? AND size_code = ?",
                    (product_id, color, size),
                )
                session.execute(
                    """
                    INSERT INTO inventory_balances(variant_id, on_hand, reserved, updated_at)
                    VALUES (?, 0, 0, ?) ON CONFLICT(variant_id) DO NOTHING
                    """,
                    (variant["id"], now),
                )
        session.execute(
            "UPDATE products SET updated_at = ? WHERE id = ?", (now, product_id)
        )
        return {
            **product,
            "sizes": selected_sizes,
            "colors": selected_colors,
        }


def create_inventory_product(
    name: str,
    sizes: list[str] | tuple[str, ...] | set[str],
    colors: list[str] | tuple[str, ...] | set[str],
) -> dict[str, Any]:
    product = resolve_product(name, create=True)
    if not product:
        raise ValueError("Потрібна назва товару")
    if not int(product.get("active") or 0):
        with _LOCK, _transaction() as session:
            session.execute(
                "UPDATE products SET active = 1, updated_at = ? WHERE id = ?",
                (_now(), product["id"]),
            )
    return configure_product_variants(product["id"], sizes, colors)


def archive_inventory_product(product_id: str) -> dict[str, Any]:
    """Hide an empty product while preserving its sales and stock history."""

    now = _now()
    with _LOCK, _transaction() as session:
        product = session.one(
            "SELECT * FROM products WHERE id = ? AND active = 1",
            (product_id,),
        )
        if not product:
            raise ValueError("Товар не знайдено")
        balance = session.one(
            """
            SELECT COALESCE(SUM(COALESCE(b.on_hand, 0)), 0) AS on_hand,
                   COALESCE(SUM(COALESCE(b.reserved, 0)), 0) AS reserved
            FROM product_variants v
            LEFT JOIN inventory_balances b ON b.variant_id = v.id
            WHERE v.product_id = ?
            """,
            (product_id,),
        )
        on_hand = int((balance or {}).get("on_hand") or 0)
        reserved = int((balance or {}).get("reserved") or 0)
        if on_hand or reserved:
            raise ValueError(
                "Спочатку спиши весь товар зі складу. Видалити можна лише товар із нульовим залишком"
            )
        session.execute(
            "UPDATE product_variants SET active = 0, updated_at = ? WHERE product_id = ?",
            (now, product_id),
        )
        session.execute(
            "UPDATE products SET active = 0, updated_at = ? WHERE id = ?",
            (now, product_id),
        )
        return product


def get_inventory_product(product_id: str) -> dict[str, Any] | None:
    with _LOCK, _transaction() as session:
        return session.one(
            "SELECT * FROM products WHERE id = ? AND active = 1",
            (product_id,),
        )


def get_product_variant_options(product_id: str) -> dict[str, list[str]]:
    with _LOCK, _transaction() as session:
        rows = session.all(
            "SELECT color_code, size_code FROM product_variants "
            "WHERE product_id = ? AND active = 1",
            (product_id,),
        )
    return {
        "sizes": [
            size for size in SIZE_CODES if any(row["size_code"] == size for row in rows)
        ],
        "colors": [
            color
            for color in COLOR_LABELS
            if any(row["color_code"] == color for row in rows)
        ],
    }


def _validated_transport_mode(value: str) -> str:
    mode = str(value or "").strip().upper()
    if mode not in TRANSPORT_LABELS:
        raise ValueError("Обери море або літак")
    return mode


def set_product_transport_cost(
    product_id: str,
    transport_mode: str,
    unit_cost: Any,
    actor_user_id: int = 0,
    actor_name: str = "",
) -> dict[str, Any]:
    """Store the default landed unit cost for future receipts."""

    mode = _validated_transport_mode(transport_mode)
    cost_cents = money_to_cents(unit_cost)
    if cost_cents <= 0:
        raise ValueError("Собівартість має бути більшою за нуль")
    now = _now()
    with _LOCK, _transaction() as session:
        product = session.one(
            "SELECT * FROM products WHERE id = ? AND active = 1",
            (product_id,),
        )
        if not product:
            raise ValueError("Товар не знайдено")
        session.execute(
            """
            INSERT INTO product_cost_profiles(
                product_id, transport_mode, unit_cost_cents, updated_by,
                updated_by_name, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id, transport_mode) DO UPDATE SET
                unit_cost_cents = excluded.unit_cost_cents,
                updated_by = excluded.updated_by,
                updated_by_name = excluded.updated_by_name,
                updated_at = excluded.updated_at
            """,
            (
                product_id,
                mode,
                cost_cents,
                int(actor_user_id or 0),
                actor_name.strip()[:200],
                now,
                now,
            ),
        )
        return (
            session.one(
                "SELECT * FROM product_cost_profiles "
                "WHERE product_id = ? AND transport_mode = ?",
                (product_id, mode),
            )
            or {}
        )


def get_product_transport_costs(product_id: str) -> dict[str, int]:
    with _LOCK, _transaction() as session:
        rows = session.all(
            "SELECT transport_mode, unit_cost_cents FROM product_cost_profiles "
            "WHERE product_id = ?",
            (product_id,),
        )
    return {str(row["transport_mode"]): int(row["unit_cost_cents"]) for row in rows}


def receive_inventory_batch(
    variant_id: str,
    transport_mode: str,
    quantity: int,
    actor_user_id: int,
    actor_name: str = "",
) -> dict[str, Any]:
    """Receive one priced batch and increase stock in the same transaction."""

    mode = _validated_transport_mode(transport_mode)
    amount = int(quantity)
    if amount <= 0 or amount > 999999:
        raise ValueError("Кількість має бути від 1 до 999999")
    now = _now()
    batch_id = uuid.uuid4().hex[:20]
    movement_id = uuid.uuid4().hex[:20]
    with _LOCK, _transaction() as session:
        variant = session.one(
            """
            SELECT v.id, v.product_id, p.canonical_name AS product_name,
                   v.color_code, v.size_code
            FROM product_variants v
            JOIN products p ON p.id = v.product_id
            WHERE v.id = ? AND v.active = 1
            """,
            (variant_id,),
        )
        if not variant:
            raise ValueError("Варіант товару не знайдено")
        profile = session.one(
            "SELECT unit_cost_cents FROM product_cost_profiles "
            "WHERE product_id = ? AND transport_mode = ?",
            (variant["product_id"], mode),
        )
        if not profile:
            raise ValueError(
                f"Спочатку вкажи собівартість для способу «{TRANSPORT_LABELS[mode]}»"
            )
        unit_cost_cents = int(profile["unit_cost_cents"])
        session.execute(
            """
            INSERT INTO inventory_batches(
                id, variant_id, transport_mode, unit_cost_cents,
                received_quantity, remaining_quantity, actor_user_id,
                actor_name, received_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                variant_id,
                mode,
                unit_cost_cents,
                amount,
                amount,
                int(actor_user_id or 0),
                actor_name.strip()[:200],
                now,
                now,
            ),
        )
        session.execute(
            """
            INSERT INTO inventory_balances(variant_id, on_hand, reserved, updated_at)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(variant_id) DO UPDATE SET
                on_hand = inventory_balances.on_hand + excluded.on_hand,
                updated_at = excluded.updated_at
            """,
            (variant_id, amount, now),
        )
        balance = session.one(
            "SELECT on_hand FROM inventory_balances WHERE variant_id = ?",
            (variant_id,),
        ) or {"on_hand": amount}
        session.execute(
            """
            INSERT INTO inventory_movements(
                id, variant_id, order_id, batch_id, quantity_delta,
                balance_after, movement_type, note, actor_user_id,
                actor_name, created_at
            ) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                movement_id,
                variant_id,
                batch_id,
                amount,
                int(balance["on_hand"]),
                f"RECEIPT_{mode}",
                f"{TRANSPORT_LABELS[mode]}; {format_money(unit_cost_cents)} грн/шт.",
                int(actor_user_id or 0),
                actor_name.strip()[:200],
                now,
            ),
        )
    result = get_inventory_variant(variant_id)
    if not result:
        raise ValueError("Варіант товару не знайдено")
    return {
        **result,
        "batch_id": batch_id,
        "movement_id": movement_id,
        "transport_mode": mode,
        "unit_cost_cents": unit_cost_cents,
        "received_quantity": amount,
        "batch_cost_cents": unit_cost_cents * amount,
    }


def list_inventory_batches(
    variant_id: str | None = None,
    *,
    remaining_only: bool = False,
) -> list[dict[str, Any]]:
    filters: list[str] = []
    params: list[Any] = []
    if variant_id:
        filters.append("b.variant_id = ?")
        params.append(variant_id)
    if remaining_only:
        filters.append("b.remaining_quantity > 0")
    where = "WHERE " + " AND ".join(filters) if filters else ""
    with _LOCK, _transaction() as session:
        return session.all(
            f"""
            SELECT b.*, v.product_id, p.canonical_name AS product_name,
                   v.color_code, v.size_code
            FROM inventory_batches b
            JOIN product_variants v ON v.id = b.variant_id
            JOIN products p ON p.id = v.product_id
            {where}
            ORDER BY b.received_at, b.id
            """,
            tuple(params),
        )


def list_inventory_products(limit: int = 200) -> list[dict[str, Any]]:
    with _LOCK, _transaction() as session:
        return session.all(
            """
            SELECT p.id, p.canonical_name,
                   COUNT(s.variant_id) AS variant_count,
                   COALESCE(SUM(COALESCE(s.on_hand, 0)), 0) AS on_hand,
                   COALESCE(SUM(COALESCE(s.reserved, 0)), 0) AS reserved,
                   COALESCE(SUM(COALESCE(s.awaiting_shipment, 0)), 0)
                       AS awaiting_shipment,
                   COALESCE(SUM(COALESCE(s.in_transit, 0)), 0) AS in_transit,
                   COALESCE(SUM(COALESCE(s.available, 0)), 0) AS available
            FROM products p
            LEFT JOIN v_inventory_stock s ON s.product_id = p.id
            WHERE p.active = 1
            GROUP BY p.id, p.canonical_name
            ORDER BY p.canonical_name
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        )


def list_product_variants(
    product_id: str, *, active_only: bool = True
) -> list[dict[str, Any]]:
    active_filter = "AND v.active = 1" if active_only else ""
    with _LOCK, _transaction() as session:
        return session.all(
            f"""
            SELECT v.id, v.product_id, p.canonical_name AS product_name,
                   v.color_code, v.size_code, v.active,
                   COALESCE(s.on_hand, 0) AS on_hand,
                   COALESCE(s.reserved, 0) AS reserved,
                   COALESCE(s.awaiting_shipment, 0) AS awaiting_shipment,
                   COALESCE(s.in_transit, 0) AS in_transit,
                   COALESCE(s.available, 0) AS available,
                   s.updated_at
            FROM product_variants v
            JOIN products p ON p.id = v.product_id
            LEFT JOIN v_inventory_stock s ON s.variant_id = v.id
            WHERE v.product_id = ? {active_filter}
            ORDER BY CASE v.color_code
                         WHEN 'BLACK' THEN 1 WHEN 'WHITE' THEN 2 ELSE 3 END,
                     CASE v.size_code
                         WHEN 'XS' THEN 1 WHEN 'S' THEN 2 WHEN 'M' THEN 3
                         WHEN 'L' THEN 4 ELSE 5 END
            """,
            (product_id,),
        )


def get_inventory_variant(variant_id: str) -> dict[str, Any] | None:
    with _LOCK, _transaction() as session:
        return session.one(
            """
            SELECT v.id, v.product_id, p.canonical_name AS product_name,
                   v.color_code, v.size_code, v.active,
                   COALESCE(s.on_hand, 0) AS on_hand,
                   COALESCE(s.reserved, 0) AS reserved,
                   COALESCE(s.awaiting_shipment, 0) AS awaiting_shipment,
                   COALESCE(s.in_transit, 0) AS in_transit,
                   COALESCE(s.available, 0) AS available,
                   s.updated_at
            FROM product_variants v
            JOIN products p ON p.id = v.product_id
            LEFT JOIN v_inventory_stock s ON s.variant_id = v.id
            WHERE v.id = ?
            """,
            (variant_id,),
        )


def _reduce_fifo_batches_for_correction(
    session: Session,
    variant_id: str,
    quantity: int,
    movement_id: str,
) -> None:
    """Keep priced batch remainders aligned after a manual negative correction."""

    needed = max(0, int(quantity))
    if not needed:
        return
    batches = session.all(
        "SELECT id, remaining_quantity FROM inventory_batches "
        "WHERE variant_id = ? AND remaining_quantity > 0 "
        "ORDER BY received_at, id",
        (variant_id,),
    )
    for batch in batches:
        if needed <= 0:
            break
        take = min(needed, int(batch["remaining_quantity"]))
        session.execute(
            "UPDATE inventory_batches "
            "SET remaining_quantity = remaining_quantity - ? WHERE id = ?",
            (take, batch["id"]),
        )
        session.execute(
            "INSERT INTO inventory_movement_batches(movement_id, batch_id, quantity) "
            "VALUES (?, ?, ?)",
            (movement_id, batch["id"], take),
        )
        needed -= take


def change_inventory_stock(
    variant_id: str,
    quantity_delta: int,
    actor_user_id: int,
    actor_name: str = "",
    *,
    movement_type: str = "ADJUSTMENT",
    note: str = "",
) -> dict[str, Any]:
    delta = int(quantity_delta)
    movement = re.sub(r"[^A-Z_]", "", movement_type.upper())[:40] or "ADJUSTMENT"
    movement_id = ""
    with _LOCK, _transaction() as session:
        variant = session.one(
            "SELECT id FROM product_variants WHERE id = ? AND active = 1",
            (variant_id,),
        )
        if not variant:
            raise ValueError("Варіант товару не знайдено")
        balance = session.one(
            "SELECT * FROM inventory_balances WHERE variant_id = ?",
            (variant_id,),
        )
        if not balance:
            session.execute(
                "INSERT INTO inventory_balances(variant_id, on_hand, reserved, updated_at) "
                "VALUES (?, 0, 0, ?)",
                (variant_id, _now()),
            )
        if delta:
            now = _now()
            movement_id = uuid.uuid4().hex[:20]
            cursor = session.execute(
                """
                UPDATE inventory_balances
                SET on_hand = on_hand + ?, updated_at = ?
                WHERE variant_id = ? AND on_hand + ? >= reserved
                """,
                (delta, now, variant_id, delta),
            )
            if cursor.rowcount != 1:
                raise ValueError("Залишок не може бути меншим за нуль")
            updated = session.one(
                "SELECT * FROM inventory_balances WHERE variant_id = ?",
                (variant_id,),
            )
            session.execute(
                """
                INSERT INTO inventory_movements(
                    id, variant_id, quantity_delta, balance_after, movement_type,
                    note, actor_user_id, actor_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    movement_id,
                    variant_id,
                    delta,
                    int(updated["on_hand"]),
                    movement,
                    note.strip()[:500],
                    int(actor_user_id or 0),
                    actor_name.strip()[:200],
                    now,
                ),
            )
            if delta < 0:
                _reduce_fifo_batches_for_correction(
                    session, variant_id, -delta, movement_id
                )
    result = get_inventory_variant(variant_id)
    if not result:
        raise ValueError("Варіант товару не знайдено")
    return {**result, "movement_id": movement_id}


def set_inventory_stock(
    variant_id: str,
    quantity: int,
    actor_user_id: int,
    actor_name: str = "",
) -> dict[str, Any]:
    target = int(quantity)
    if target < 0:
        raise ValueError("Кількість не може бути від’ємною")
    current = get_inventory_variant(variant_id)
    if not current or not int(current.get("active") or 0):
        raise ValueError("Варіант товару не знайдено")
    return change_inventory_stock(
        variant_id,
        target - int(current.get("on_hand") or 0),
        actor_user_id,
        actor_name,
        movement_type="SET_BALANCE",
        note=f"Встановлено точний залишок: {target}",
    )


_REVERSIBLE_MOVEMENT_TYPES = {
    "RECEIPT",
    "RECEIPT_SEA",
    "RECEIPT_AIR",
    "WRITE_OFF",
    "SET_BALANCE",
    "ADJUSTMENT",
}


def get_last_reversible_inventory_movement() -> dict[str, Any] | None:
    """Return the newest manual stock change that has not been undone."""

    placeholders = ",".join("?" for _ in _REVERSIBLE_MOVEMENT_TYPES)
    with _LOCK, _transaction() as session:
        return session.one(
            f"""
            SELECT m.*, p.canonical_name AS product_name,
                   v.color_code, v.size_code
            FROM inventory_movements m
            JOIN product_variants v ON v.id = m.variant_id
            JOIN products p ON p.id = v.product_id
            LEFT JOIN inventory_movement_reversals r
                   ON r.original_movement_id = m.id
            WHERE m.movement_type IN ({placeholders})
              AND m.order_id = ''
              AND r.original_movement_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM inventory_movements newer
                  WHERE newer.variant_id = m.variant_id
                    AND (newer.created_at > m.created_at
                         OR (newer.created_at = m.created_at AND newer.id > m.id))
              )
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT 1
            """,
            tuple(sorted(_REVERSIBLE_MOVEMENT_TYPES)),
        )


def undo_inventory_movement(
    movement_id: str,
    actor_user_id: int,
    actor_name: str = "",
) -> dict[str, Any]:
    """Safely reverse the latest manual movement for its stock variant."""

    now = _now()
    reversal_id = uuid.uuid4().hex[:20]
    with _LOCK, _transaction() as session:
        movement = session.one(
            "SELECT * FROM inventory_movements WHERE id = ?",
            (movement_id,),
        )
        if not movement or movement["movement_type"] not in _REVERSIBLE_MOVEMENT_TYPES:
            raise ValueError("Цю складську дію не можна скасувати")
        if movement.get("order_id"):
            raise ValueError("Продажі через цю кнопку не скасовуються")
        if session.one(
            "SELECT original_movement_id FROM inventory_movement_reversals "
            "WHERE original_movement_id = ?",
            (movement_id,),
        ):
            raise ValueError("Цю дію вже скасовано")

        latest = session.one(
            "SELECT id FROM inventory_movements WHERE variant_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (movement["variant_id"],),
        )
        if not latest or latest["id"] != movement_id:
            raise ValueError(
                "Після цієї дії залишок уже змінювався. Автоматичне скасування небезпечне"
            )

        balance_sql = "SELECT * FROM inventory_balances WHERE variant_id = ?"
        if session.backend == "postgresql":
            balance_sql += " FOR UPDATE"
        balance = session.one(balance_sql, (movement["variant_id"],))
        if not balance:
            raise ValueError("Залишок товару не знайдено")
        original_delta = int(movement["quantity_delta"])
        new_balance = int(balance["on_hand"]) - original_delta
        if new_balance < int(balance["reserved"]):
            raise ValueError(
                "Частина товару вже зарезервована. Спочатку скасуй замовлення"
            )

        batch_id = str(movement.get("batch_id") or "")
        if original_delta > 0 and batch_id:
            batch = session.one(
                "SELECT * FROM inventory_batches WHERE id = ?",
                (batch_id,),
            )
            if not batch or int(batch["remaining_quantity"]) < original_delta:
                raise ValueError(
                    "Товар із цього надходження вже використовувався. Скасування недоступне"
                )
            session.execute(
                "UPDATE inventory_batches SET remaining_quantity = remaining_quantity - ? "
                "WHERE id = ?",
                (original_delta, batch_id),
            )
        elif original_delta < 0:
            allocations = session.all(
                "SELECT batch_id, quantity FROM inventory_movement_batches "
                "WHERE movement_id = ?",
                (movement_id,),
            )
            for allocation in allocations:
                session.execute(
                    "UPDATE inventory_batches SET remaining_quantity = remaining_quantity + ? "
                    "WHERE id = ?",
                    (int(allocation["quantity"]), allocation["batch_id"]),
                )

        session.execute(
            "UPDATE inventory_balances SET on_hand = ?, updated_at = ? "
            "WHERE variant_id = ?",
            (new_balance, now, movement["variant_id"]),
        )
        session.execute(
            """
            INSERT INTO inventory_movements(
                id, variant_id, quantity_delta, balance_after, movement_type,
                note, actor_user_id, actor_name, created_at
            ) VALUES (?, ?, ?, ?, 'UNDO', ?, ?, ?, ?)
            """,
            (
                reversal_id,
                movement["variant_id"],
                -original_delta,
                new_balance,
                f"Скасовано дію {movement['movement_type']}: {movement_id}",
                int(actor_user_id or 0),
                actor_name.strip()[:200],
                now,
            ),
        )
        session.execute(
            """
            INSERT INTO inventory_movement_reversals(
                original_movement_id, reversal_movement_id,
                actor_user_id, actor_name, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                movement_id,
                reversal_id,
                int(actor_user_id or 0),
                actor_name.strip()[:200],
                now,
            ),
        )

    result = get_inventory_variant(str(movement["variant_id"]))
    if not result:
        raise ValueError("Варіант товару не знайдено")
    return {
        **result,
        "undone_movement_id": movement_id,
        "reversal_movement_id": reversal_id,
        "undone_quantity_delta": original_delta,
        "undone_movement_type": movement["movement_type"],
    }


def save_inventory_wizard(
    user_id: int,
    chat_id: int,
    step: str,
    payload: dict[str, Any] | None = None,
) -> None:
    now = _now()
    with _LOCK, _transaction() as session:
        session.execute(
            """
            INSERT INTO inventory_wizards(
                user_id, chat_id, step, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                step = excluded.step,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                int(user_id),
                int(chat_id),
                step,
                json.dumps(payload or {}, ensure_ascii=False),
                now,
                now,
            ),
        )


def get_inventory_wizard(user_id: int) -> dict[str, Any] | None:
    with _LOCK, _transaction() as session:
        row = session.one(
            "SELECT * FROM inventory_wizards WHERE user_id = ?", (int(user_id),)
        )
    if row:
        row["payload"] = json.loads(row.pop("payload_json") or "{}")
    return row


def clear_inventory_wizard(user_id: int) -> None:
    with _LOCK, _transaction() as session:
        session.execute(
            "DELETE FROM inventory_wizards WHERE user_id = ?", (int(user_id),)
        )


def create_submission(
    raw_text: str,
    parsed: dict[str, Any],
    ttns: list[str],
    source_user_id: int,
    source_user_name: str,
    submission_id: str | None = None,
) -> str:
    submission_id = submission_id or uuid.uuid4().hex[:16]
    if not re.fullmatch(r"[a-f0-9]{16}", submission_id):
        raise ValueError("Invalid submission ID")
    now = _now()
    with _LOCK, _transaction() as session:
        existing = session.one("SELECT raw_text,parsed_json,ttns_json,source_user_id FROM order_submissions WHERE id=?", (submission_id,))
        if existing:
            if (existing["raw_text"] != raw_text or json.loads(existing["parsed_json"]) != parsed or json.loads(existing["ttns_json"]) != ttns or int(existing["source_user_id"]) != int(source_user_id)):
                raise ValueError("Submission identity conflict")
            return submission_id
        session.execute(
            """
            INSERT INTO order_submissions(
                id, raw_text, parsed_json, ttns_json, source_user_id,
                source_user_name, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING
            """,
            (
                submission_id,
                raw_text,
                json.dumps(parsed, ensure_ascii=False),
                json.dumps(ttns, ensure_ascii=False),
                int(source_user_id or 0),
                source_user_name,
                now,
                now,
            ),
        )
    return submission_id


def attach_submission_message(
    submission_id: str, chat_id: int, message_id: int
) -> None:
    with _LOCK, _transaction() as session:
        session.execute(
            "UPDATE order_submissions SET telegram_chat_id = ?, telegram_message_id = ?, updated_at = ? WHERE id = ?",
            (chat_id, message_id, _now(), submission_id),
        )


def get_submission(submission_id: str) -> dict[str, Any] | None:
    with _LOCK, _transaction() as session:
        row = session.one(
            "SELECT * FROM order_submissions WHERE id = ?", (submission_id,)
        )
    if row:
        row["parsed"] = json.loads(row.pop("parsed_json"))
        row["ttns"] = json.loads(row.pop("ttns_json"))
    return row


def review_submission(submission_id: str, status: str, reviewer_id: int) -> bool:
    if status not in {"CONFIRMED", "CANCELLED", "NEEDS_EDIT"}:
        raise ValueError("Unsupported submission status")
    allowed_statuses = (
        ("PENDING", "CONFIRMED") if status == "CANCELLED" else ("PENDING",)
    )
    placeholders = ",".join("?" for _ in allowed_statuses)
    with _LOCK, _transaction() as session:
        cursor = session.execute(
            f"""
            UPDATE order_submissions SET status = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ?
            WHERE id = ? AND status IN ({placeholders})
            """,
            (
                status,
                int(reviewer_id or 0),
                _now(),
                _now(),
                submission_id,
                *allowed_statuses,
            ),
        )
        return cursor.rowcount == 1


def claim_submission(submission_id: str, reviewer_id: int) -> bool:
    with _LOCK, _transaction() as session:
        cursor = session.execute(
            """
            UPDATE order_submissions SET status = 'PROCESSING', reviewed_by = ?, updated_at = ?
            WHERE id = ? AND status = 'PENDING'
            """,
            (int(reviewer_id or 0), _now(), submission_id),
        )
        return cursor.rowcount == 1


def finish_submission(submission_id: str, success: bool) -> None:
    status = "CONFIRMED" if success else "PENDING"
    with _LOCK, _transaction() as session:
        session.execute(
            """
            UPDATE order_submissions SET status = ?, reviewed_at = ?, updated_at = ?
            WHERE id = ? AND status = 'PROCESSING'
            """,
            (status, _now() if success else None, _now(), submission_id),
        )


def _event(
    session: Session,
    order_id: str,
    event_type: str,
    actor_user_id: int = 0,
    actor_name: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    session.execute(
        """
        INSERT INTO order_events(id, order_id, event_type, actor_user_id, actor_name, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex[:24],
            order_id,
            event_type,
            int(actor_user_id or 0),
            actor_name,
            json.dumps(details or {}, ensure_ascii=False),
            _now(),
        ),
    )


def _resolve_order_product(name: str) -> dict[str, Any] | None:
    """Reuse the configured base product when the order also contains a variant."""

    base_key, _, _ = _inventory_variant_hints(name)
    if base_key:
        with _LOCK, _transaction() as session:
            products = session.all(
                """
                SELECT DISTINCT p.*
                FROM products p
                LEFT JOIN product_aliases a ON a.product_id = p.id
                JOIN product_variants v ON v.product_id = p.id AND v.active = 1
                WHERE p.active = 1
                  AND (p.normalized_key = ? OR a.alias_key = ?)
                """,
                (base_key, base_key),
            )
        if len(products) == 1:
            return products[0]
    return resolve_product(name or "Невідомий товар", create=True)


def record_order(
    *,
    ttn: str,
    sale_type_label: str,
    carrier: str,
    product_name: str,
    quantity: int = 1,
    sale_price: Any = None,
    purchase_cost: Any = None,
    delivery_cost: Any = None,
    platform_fee: Any = None,
    other_cost: Any = None,
    olx_order_id: str = "",
    source_user_id: int = 0,
    source_user_name: str = "",
    initial_status: str = "CONFIRMED",
) -> dict[str, Any]:
    sale_type = SALE_TYPES.get(sale_type_label, "COD")
    with _LOCK, _transaction() as session:
        imported = session.all("SELECT DISTINCT product_id FROM repliq_parcel_items WHERE ttn=?", (ttn,))
        product = session.one("SELECT * FROM products WHERE id=?", (imported[0]["product_id"],)) if len(imported) == 1 else None
    if not imported:
        product = _resolve_order_product(product_name or "Невідомий товар")
    now = _now()
    shipped_at = now if initial_status == "SHIPPED" else None
    sold_at = now if initial_status == "DELIVERED" else None
    with _LOCK, _transaction() as session:
        existing = session.one("SELECT * FROM orders WHERE ttn = ?", (ttn,))
        manual_purchase_cost = money_to_cents(purchase_cost)
        cost_source = str((existing or {}).get("cost_source") or "")
        cost_calculated_at = (existing or {}).get("cost_calculated_at")
        if manual_purchase_cost > 0:
            cost_source = "MANUAL"
            cost_calculated_at = now
        values = {
            "olx_order_id": olx_order_id or (existing or {}).get("olx_order_id", ""),
            "sale_type": sale_type,
            "carrier": carrier or (existing or {}).get("carrier", ""),
            "product_id": product["id"]
            if product
            else (existing or {}).get("product_id"),
            "raw_product_name": product_name
            or (existing or {}).get("raw_product_name", ""),
            "quantity": max(1, int(quantity or 1)),
            "sale_price_cents": money_to_cents(sale_price)
            or int((existing or {}).get("sale_price_cents", 0)),
            "purchase_cost_cents": manual_purchase_cost
            or int((existing or {}).get("purchase_cost_cents", 0)),
            "delivery_cost_cents": money_to_cents(delivery_cost)
            or int((existing or {}).get("delivery_cost_cents", 0)),
            "platform_fee_cents": money_to_cents(platform_fee)
            or int((existing or {}).get("platform_fee_cents", 0)),
            "other_cost_cents": money_to_cents(other_cost)
            or int((existing or {}).get("other_cost_cents", 0)),
            "cost_source": cost_source,
            "cost_calculated_at": cost_calculated_at,
            "source_user_id": int(
                source_user_id or (existing or {}).get("source_user_id", 0)
            ),
            "source_user_name": source_user_name
            or (existing or {}).get("source_user_name", ""),
        }
        if imported:
            totals = session.one("SELECT SUM(quantity) AS quantity,SUM(quantity*unit_price_cents) AS total FROM repliq_parcel_items WHERE ttn=?", (ttn,))
            values["quantity"], values["sale_price_cents"] = int(totals["quantity"]), int(totals["total"])
        if existing:
            session.execute(
                """
                UPDATE orders SET olx_order_id = ?, sale_type = ?, carrier = ?, product_id = ?,
                    raw_product_name = ?, quantity = ?, sale_price_cents = ?, purchase_cost_cents = ?,
                    delivery_cost_cents = ?, platform_fee_cents = ?, other_cost_cents = ?,
                    cost_source = ?, cost_calculated_at = ?,
                    source_user_id = ?, source_user_name = ?, updated_at = ?
                WHERE id = ?
                """,
                (*values.values(), now, existing["id"]),
            )
            order_id = existing["id"]
            event_type = "ORDER_UPDATED"
        else:
            order_id = uuid.uuid4().hex[:24]
            session.execute(
                """
                INSERT INTO orders(
                    id, ttn, olx_order_id, sale_type, carrier, product_id, raw_product_name,
                    quantity, sale_price_cents, purchase_cost_cents, delivery_cost_cents,
                    platform_fee_cents, other_cost_cents, status, shipped_at, sold_at,
                    cost_source, cost_calculated_at,
                    source_user_id, source_user_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    ttn,
                    values["olx_order_id"],
                    values["sale_type"],
                    values["carrier"],
                    values["product_id"],
                    values["raw_product_name"],
                    values["quantity"],
                    values["sale_price_cents"],
                    values["purchase_cost_cents"],
                    values["delivery_cost_cents"],
                    values["platform_fee_cents"],
                    values["other_cost_cents"],
                    initial_status,
                    shipped_at,
                    sold_at,
                    values["cost_source"],
                    values["cost_calculated_at"],
                    values["source_user_id"],
                    values["source_user_name"],
                    now,
                    now,
                ),
            )
            event_type = "ORDER_CREATED"
        _event(session, order_id, event_type, source_user_id, source_user_name)
        saved_order = (
            session.one("SELECT * FROM orders WHERE id = ?", (order_id,)) or {}
        )
        reservation_result: dict[str, Any] = {}
        if initial_status == "SHIPPED":
            reservation_result = _reserve_order_inventory(
                session,
                saved_order,
                source_user_id,
                source_user_name,
            )
        result = session.one(
            """
            SELECT o.*, p.canonical_name AS product_name FROM orders o
            LEFT JOIN products p ON p.id = o.product_id WHERE o.id = ?
            """,
            (order_id,),
        )
        return {**(result or {}), **reservation_result}


def get_order_by_ttn(ttn: str) -> dict[str, Any] | None:
    with _LOCK, _transaction() as session:
        return session.one(
            """
            SELECT o.*, p.canonical_name AS product_name FROM orders o
            LEFT JOIN products p ON p.id = o.product_id WHERE o.ttn = ?
            """,
            (ttn,),
        )


def get_order(order_id: str) -> dict[str, Any] | None:
    with _LOCK, _transaction() as session:
        return session.one(
            """
            SELECT o.*, p.canonical_name AS product_name FROM orders o
            LEFT JOIN products p ON p.id = o.product_id WHERE o.id = ?
            """,
            (order_id,),
        )


def reserve_order_by_ttn(
    ttn: str,
    actor_user_id: int,
    actor_name: str = "",
) -> dict[str, Any] | None:
    """Reserve stock for a confirmed order without marking it as shipped."""

    with _LOCK, _transaction() as session:
        order = session.one("SELECT * FROM orders WHERE ttn = ?", (ttn,))
        if not order:
            return None
        if order["status"] in {"CANCELLED", "CLOSED", "RETURNED"}:
            return {
                **order,
                "reservation_status": "not_allowed",
                "reserved_quantity": 0,
                "warning": "Це замовлення вже закрите",
            }
        reservation = _reserve_order_inventory(
            session,
            order,
            actor_user_id,
            actor_name,
        )
        _event(
            session,
            order["id"],
            "ORDER_CONFIRMED_AND_RESERVED",
            actor_user_id,
            actor_name,
            reservation,
        )
        result = session.one(
            """
            SELECT o.*, p.canonical_name AS product_name FROM orders o
            LEFT JOIN products p ON p.id = o.product_id WHERE o.id = ?
            """,
            (order["id"],),
        )
        return {**(result or {}), **reservation}


def update_order_status_by_ttn(
    ttn: str,
    status: str,
    *,
    actor_user_id: int = 0,
    actor_name: str = "",
    reason: str = "",
) -> dict[str, Any] | None:
    if status not in ORDER_STATUSES:
        raise ValueError("Unsupported order status")
    now = _now()
    reservation_result: dict[str, Any] = {}
    with _LOCK, _transaction() as session:
        order = session.one("SELECT * FROM orders WHERE ttn = ?", (ttn,))
        if not order:
            return None
        shipped_at = (
            now
            if status == "SHIPPED" and not order.get("shipped_at")
            else order.get("shipped_at")
        )
        sold_at = (
            now
            if status == "DELIVERED" and not order.get("sold_at")
            else order.get("sold_at")
        )
        refused_at = (
            now
            if status == "REFUSED" and not order.get("refused_at")
            else order.get("refused_at")
        )
        returned_at = (
            now
            if status == "RETURNED" and not order.get("returned_at")
            else order.get("returned_at")
        )
        session.execute(
            """
            UPDATE orders SET status = ?, shipped_at = ?, sold_at = ?, refused_at = ?, returned_at = ?,
                return_reason = ?, updated_at = ? WHERE id = ?
            """,
            (
                status,
                shipped_at,
                sold_at,
                refused_at,
                returned_at,
                reason or order.get("return_reason", ""),
                now,
                order["id"],
            ),
        )
        _event(
            session,
            order["id"],
            f"STATUS_{status}",
            actor_user_id,
            actor_name,
            {"previous_status": order["status"], "reason": reason},
        )
        updated_order = session.one("SELECT * FROM orders WHERE id = ?", (order["id"],))
        if status in {"SHIPPED", "DELIVERED", "REFUSED", "RESHIP_REQUIRED"}:
            reservation_result = _reserve_order_inventory(
                session,
                updated_order,
                actor_user_id,
                actor_name,
            )
        elif status in {"RETURNED", "CANCELLED", "CLOSED"}:
            reservation_result = _release_order_inventory_reservation(
                session,
                updated_order,
                actor_user_id,
                actor_name,
            )
    result = get_order(order["id"])
    return {**(result or {}), **reservation_result} if result else None


def mark_order_shipped_by_ttn(
    ttn: str,
    actor_user_id: int,
    actor_name: str = "",
) -> dict[str, Any] | None:
    """Confirm the real hand-off to the carrier without regressing final states."""

    now = _now()
    terminal_statuses = {"DELIVERED", "REFUSED", "RETURNED", "CANCELLED", "CLOSED"}
    with _LOCK, _transaction() as session:
        order = session.one("SELECT * FROM orders WHERE ttn = ?", (ttn,))
        if not order:
            return None
        if order["status"] not in terminal_statuses:
            session.execute(
                "UPDATE orders SET status = 'SHIPPED', shipped_at = COALESCE(shipped_at, ?), "
                "updated_at = ? WHERE id = ?",
                (now, now, order["id"]),
            )
        updated_order = session.one("SELECT * FROM orders WHERE id = ?", (order["id"],))
        reservation_result = (
            _reserve_order_inventory(
                session,
                updated_order,
                actor_user_id,
                actor_name,
            )
            if order["status"] not in terminal_statuses
            else {}
        )
        _event(
            session,
            order["id"],
            "SHIPMENT_CONFIRMED_BY_USER",
            actor_user_id,
            actor_name,
            {"previous_status": order["status"]},
        )
        result = session.one(
            """
            SELECT o.*, p.canonical_name AS product_name FROM orders o
            LEFT JOIN products p ON p.id = o.product_id WHERE o.id = ?
            """,
            (order["id"],),
        )
        return {**(result or {}), **reservation_result} if result else None


def update_order_status(
    order_id: str,
    status: str,
    actor_user_id: int = 0,
    actor_name: str = "",
) -> dict[str, Any] | None:
    order = get_order(order_id)
    if not order:
        return None
    return update_order_status_by_ttn(
        order["ttn"], status, actor_user_id=actor_user_id, actor_name=actor_name
    )


def _inventory_variant_hints(text: str) -> tuple[str, str | None, str | None]:
    """Extract a base product key plus optional size and color from free text."""

    size: str | None = None
    color: str | None = None
    product_tokens: list[str] = []
    ignored = {"колір", "цвет", "color", "розмір", "размер", "size"}
    for token in re.findall(r"[A-Za-zА-ЯІЇЄҐа-яіїєґ0-9]+", text or ""):
        folded = token.casefold()
        upper = token.upper()
        if upper in SIZE_CODES:
            size = upper
        elif folded in COLOR_WORDS:
            color = COLOR_WORDS[folded]
        elif folded not in ignored:
            product_tokens.append(token)
    return normalize_product_key(" ".join(product_tokens)), size, color


def _match_inventory_variant(
    session: Session,
    order: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    raw_name = str(order.get("raw_product_name") or "").strip()
    base_key, size, color = _inventory_variant_hints(raw_name)
    products = session.all(
        """
        SELECT DISTINCT p.id, p.canonical_name, p.normalized_key
        FROM products p
        JOIN product_variants v ON v.product_id = p.id AND v.active = 1
        WHERE p.active = 1
        """
    )
    if not products:
        return None, "У каталозі складу ще немає налаштованих товарів"

    aliases = session.all("SELECT product_id, alias_key FROM product_aliases")
    keys_by_product: dict[str, set[str]] = {
        row["id"]: {row["normalized_key"]} for row in products
    }
    for alias in aliases:
        if alias["product_id"] in keys_by_product:
            keys_by_product[alias["product_id"]].add(alias["alias_key"])

    product_ids = [
        product_id
        for product_id, keys in keys_by_product.items()
        if base_key and base_key in keys
    ]
    if not product_ids and order.get("product_id") in keys_by_product:
        product_ids = [str(order["product_id"])]
    if not product_ids and base_key:
        base_tokens = set(base_key.split())
        scored: list[tuple[float, str]] = []
        for product_id, keys in keys_by_product.items():
            score = max(
                (
                    len(base_tokens.intersection(set(key.split())))
                    / max(len(base_tokens), len(set(key.split())))
                )
                for key in keys
                if key
            )
            if score >= 0.66 and len(base_tokens) >= 2:
                scored.append((score, product_id))
        scored.sort(reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            product_ids = [scored[0][1]]

    if len(product_ids) != 1:
        return None, "Не вдалося однозначно знайти товар за описом"

    variants = session.all(
        """
        SELECT v.id, v.product_id, p.canonical_name AS product_name,
               v.color_code, v.size_code,
               COALESCE(b.on_hand, 0) AS on_hand,
               COALESCE(b.reserved, 0) AS reserved
        FROM product_variants v
        JOIN products p ON p.id = v.product_id
        LEFT JOIN inventory_balances b ON b.variant_id = v.id
        WHERE v.product_id = ? AND v.active = 1
        """,
        (product_ids[0],),
    )
    if size:
        variants = [row for row in variants if row["size_code"] == size]
    if color:
        variants = [row for row in variants if row["color_code"] == color]
    if len(variants) == 1:
        return variants[0], ""

    missing: list[str] = []
    if not color:
        missing.append("колір")
    if not size:
        missing.append("розмір")
    if missing:
        return None, "В описі не вистачає: " + " і ".join(missing)
    return None, "Для цього кольору й розміру варіант товару не налаштований"


def _reserve_order_inventory(
    session: Session,
    order: dict[str, Any],
    actor_user_id: int = 0,
    actor_name: str = "",
) -> dict[str, Any]:
    """Move an order's stock from available to in-transit exactly once."""
    from parcel_manifest import reserve
    imported = reserve(session, order, actor_user_id, actor_name)
    if imported is not None:
        return imported

    existing = session.one(
        "SELECT * FROM inventory_reservations WHERE order_id = ?",
        (order["id"],),
    )
    if existing and existing["status"] == "ACTIVE":
        return {
            "reservation_status": "already_reserved",
            "reserved_quantity": int(existing["quantity"]),
            "variant_id": existing["variant_id"],
        }
    if order.get("onec_recorded_at") or (existing and existing["status"] == "CONSUMED"):
        return {"reservation_status": "already_finished", "reserved_quantity": 0}

    variant, reason = _match_inventory_variant(session, order)
    quantity = max(1, int(order.get("quantity") or 1))
    if not variant:
        return {
            "reservation_status": "unmatched",
            "reserved_quantity": 0,
            "warning": reason,
        }

    on_hand = int(variant.get("on_hand") or 0)
    in_transit = int(variant.get("reserved") or 0)
    available = max(0, on_hand - in_transit)
    if available < quantity:
        return {
            "reservation_status": "shortage",
            "reserved_quantity": 0,
            "variant_id": variant["id"],
            "product_name": variant["product_name"],
            "color_code": variant["color_code"],
            "size_code": variant["size_code"],
            "warning": (
                f"Потрібно {quantity} шт., а вільно лише {available}. "
                "Товар не резервувався"
            ),
        }
    reserve_quantity = quantity

    now = _now()
    cursor = session.execute(
        """
        UPDATE inventory_balances
        SET reserved = reserved + ?, updated_at = ?
        WHERE variant_id = ? AND on_hand - reserved >= ?
        """,
        (reserve_quantity, now, variant["id"], reserve_quantity),
    )
    if cursor.rowcount != 1:
        return {
            "reservation_status": "shortage",
            "reserved_quantity": 0,
            "variant_id": variant["id"],
            "warning": "Залишок уже змінився. Перевір склад",
        }
    session.execute(
        """
        INSERT INTO inventory_reservations(
            order_id, variant_id, quantity, status, actor_user_id, actor_name,
            reserved_at, released_at, updated_at
        ) VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, NULL, ?)
        ON CONFLICT(order_id) DO UPDATE SET
            variant_id = excluded.variant_id,
            quantity = excluded.quantity,
            status = 'ACTIVE',
            actor_user_id = excluded.actor_user_id,
            actor_name = excluded.actor_name,
            reserved_at = excluded.reserved_at,
            released_at = NULL,
            updated_at = excluded.updated_at
        """,
        (
            order["id"],
            variant["id"],
            reserve_quantity,
            int(actor_user_id or 0),
            actor_name.strip()[:200],
            now,
            now,
        ),
    )
    details = {
        "reservation_status": "reserved",
        "variant_id": variant["id"],
        "requested_quantity": quantity,
        "reserved_quantity": reserve_quantity,
        "available_after": available - reserve_quantity,
    }
    _event(
        session,
        order["id"],
        "INVENTORY_RESERVED_IN_TRANSIT",
        actor_user_id,
        actor_name,
        details,
    )
    return {
        **details,
        "product_name": variant["product_name"],
        "color_code": variant["color_code"],
        "size_code": variant["size_code"],
        "warning": "",
    }


def _release_order_inventory_reservation(
    session: Session,
    order: dict[str, Any],
    actor_user_id: int = 0,
    actor_name: str = "",
) -> dict[str, Any]:
    """Return an active in-transit reservation to available stock."""
    from parcel_manifest import release
    imported = release(session, order, actor_user_id, actor_name)
    if imported is not None:
        return imported

    reservation = session.one(
        "SELECT * FROM inventory_reservations WHERE order_id = ? AND status = 'ACTIVE'",
        (order["id"],),
    )
    if not reservation:
        return {"reservation_status": "not_active", "released_quantity": 0}
    now = _now()
    balance = session.one(
        "SELECT reserved FROM inventory_balances WHERE variant_id = ?",
        (reservation["variant_id"],),
    )
    released = min(
        int(reservation["quantity"]),
        int((balance or {}).get("reserved") or 0),
    )
    session.execute(
        "UPDATE inventory_balances SET reserved = reserved - ?, updated_at = ? "
        "WHERE variant_id = ?",
        (released, now, reservation["variant_id"]),
    )
    session.execute(
        "UPDATE inventory_reservations SET status = 'RELEASED', released_at = ?, "
        "updated_at = ? WHERE order_id = ?",
        (now, now, order["id"]),
    )
    details = {
        "reservation_status": "released",
        "variant_id": reservation["variant_id"],
        "released_quantity": released,
    }
    _event(
        session,
        order["id"],
        "INVENTORY_RESERVATION_RELEASED",
        actor_user_id,
        actor_name,
        details,
    )
    return details


def _consume_fifo_batches(
    session: Session,
    variant_id: str,
    order_id: str,
    quantity: int,
    now: str,
) -> dict[str, Any]:
    """Allocate the oldest priced batches to an order."""

    needed = max(0, int(quantity))
    allocated = 0
    total_cost_cents = 0
    modes: list[str] = []
    batches = session.all(
        """
        SELECT * FROM inventory_batches
        WHERE variant_id = ? AND remaining_quantity > 0
        ORDER BY received_at, id
        """,
        (variant_id,),
    )
    for batch in batches:
        if needed <= 0:
            break
        take = min(needed, int(batch["remaining_quantity"]))
        cursor = session.execute(
            """
            UPDATE inventory_batches
            SET remaining_quantity = remaining_quantity - ?
            WHERE id = ? AND remaining_quantity >= ?
            """,
            (take, batch["id"], take),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Партію вже змінено. Спробуй ще раз.")
        unit_cost = int(batch["unit_cost_cents"])
        line_total = unit_cost * take
        session.execute(
            """
            INSERT INTO inventory_batch_allocations(
                id, batch_id, order_id, quantity, unit_cost_cents,
                total_cost_cents, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex[:20],
                batch["id"],
                order_id,
                take,
                unit_cost,
                line_total,
                now,
            ),
        )
        allocated += take
        needed -= take
        total_cost_cents += line_total
        mode = str(batch["transport_mode"])
        if mode not in modes:
            modes.append(mode)
    return {
        "allocated_quantity": allocated,
        "total_cost_cents": total_cost_cents,
        "transport_modes": modes,
    }


def _profit_fields(
    order: dict[str, Any],
    purchase_cost_cents: int,
    cost_source: str,
) -> dict[str, Any]:
    sale_price = int(order.get("sale_price_cents") or 0)
    total_cost = (
        int(purchase_cost_cents)
        + int(order.get("delivery_cost_cents") or 0)
        + int(order.get("platform_fee_cents") or 0)
        + int(order.get("other_cost_cents") or 0)
    )
    cost_ready = cost_source in {"FIFO_BATCH", "MANUAL"}
    profit_ready = cost_ready and sale_price > 0
    return {
        "sale_price_cents": sale_price,
        "purchase_cost_cents": int(purchase_cost_cents),
        "total_cost_cents": total_cost,
        "profit_cents": sale_price - total_cost if profit_ready else None,
        "cost_source": cost_source,
        "cost_ready": cost_ready,
        "profit_ready": profit_ready,
    }


def mark_order_in_1c(
    order_id: str,
    actor_user_id: int,
    actor_name: str = "",
) -> dict[str, Any] | None:
    """Mark an order in 1C and write its exact stock variant off once."""

    with _LOCK, _transaction() as session:
        order = session.one("SELECT * FROM orders WHERE id = ?", (order_id,))
        if not order:
            return None
        if order.get("onec_recorded_at"):
            financial = _profit_fields(
                order,
                int(order.get("purchase_cost_cents") or 0),
                str(order.get("cost_source") or ""),
            )
            return {
                "ok": True,
                "already_recorded": True,
                "inventory_status": "already_recorded",
                "ttn": order.get("ttn") or "",
                **financial,
            }
        from parcel_manifest import record_in_1c
        imported = record_in_1c(session, order, actor_user_id, actor_name)
        if imported is not None:
            return imported
        now = _now()
        cursor = session.execute(
            "UPDATE orders SET onec_recorded_at = ?, onec_recorded_by = ?, updated_at = ? "
            "WHERE id = ? AND onec_recorded_at IS NULL",
            (now, int(actor_user_id or 0), now, order_id),
        )
        if cursor.rowcount != 1:
            return {
                "ok": True,
                "already_recorded": True,
                "inventory_status": "already_recorded",
                "ttn": order.get("ttn") or "",
            }

        reservation = session.one(
            "SELECT * FROM inventory_reservations "
            "WHERE order_id = ? AND status = 'ACTIVE'",
            (order_id,),
        )
        if reservation:
            variant = session.one(
                """
                SELECT v.id, v.product_id, p.canonical_name AS product_name,
                       v.color_code, v.size_code,
                       COALESCE(b.on_hand, 0) AS on_hand,
                       COALESCE(b.reserved, 0) AS reserved
                FROM product_variants v
                JOIN products p ON p.id = v.product_id
                LEFT JOIN inventory_balances b ON b.variant_id = v.id
                WHERE v.id = ?
                """,
                (reservation["variant_id"],),
            )
            reason = "" if variant else "Зарезервований товар не знайдено"
        else:
            variant, reason = _match_inventory_variant(session, order)
        quantity = max(1, int(order.get("quantity") or 1))
        if not variant:
            cost_source = str(order.get("cost_source") or "")
            if cost_source != "MANUAL":
                cost_source = "MISSING"
                session.execute(
                    "UPDATE orders SET cost_source = ?, cost_calculated_at = ?, updated_at = ? "
                    "WHERE id = ?",
                    (cost_source, now, now, order_id),
                )
            financial = _profit_fields(
                order,
                int(order.get("purchase_cost_cents") or 0),
                cost_source,
            )
            warnings = [reason]
            if not financial["cost_ready"]:
                warnings.append("Собівартість не визначена")
            if int(order.get("sale_price_cents") or 0) <= 0:
                warnings.append("Не вказана ціна продажу")
            details = {
                "inventory_status": "unmatched",
                "inventory_reason": reason,
                "raw_product_name": order.get("raw_product_name") or "",
                "quantity": quantity,
                "cost_source": cost_source,
            }
            _event(
                session,
                order_id,
                "RECORDED_IN_1C",
                actor_user_id,
                actor_name,
                details,
            )
            return {
                "ok": True,
                "already_recorded": False,
                "inventory_status": "unmatched",
                "warning": ". ".join(warnings),
                "product_name": order.get("raw_product_name") or "Невідомий товар",
                "quantity": quantity,
                "ttn": order.get("ttn") or "",
                **financial,
            }

        on_hand = int(variant.get("on_hand") or 0)
        in_transit = int(variant.get("reserved") or 0)
        reserved_for_order = min(
            int((reservation or {}).get("quantity") or 0),
            in_transit,
        )
        available_for_order = max(0, on_hand - in_transit + reserved_for_order)
        deducted = min(available_for_order, quantity)
        balance_after = on_hand - deducted
        in_transit_after = max(0, in_transit - reserved_for_order)
        available_after = balance_after - in_transit_after
        session.execute(
            "UPDATE inventory_balances SET on_hand = ?, reserved = ?, updated_at = ? "
            "WHERE variant_id = ?",
            (balance_after, in_transit_after, now, variant["id"]),
        )
        if reservation:
            session.execute(
                "UPDATE inventory_reservations SET status = 'CONSUMED', released_at = ?, "
                "updated_at = ? WHERE order_id = ?",
                (now, now, order_id),
            )
        allocation = _consume_fifo_batches(
            session,
            variant["id"],
            order_id,
            deducted,
            now,
        )
        manual_cost = (
            str(order.get("cost_source") or "") == "MANUAL"
            and int(order.get("purchase_cost_cents") or 0) > 0
        )
        if manual_cost:
            purchase_cost_cents = int(order["purchase_cost_cents"])
            cost_source = "MANUAL"
        elif int(allocation["allocated_quantity"]) == quantity:
            purchase_cost_cents = int(allocation["total_cost_cents"])
            cost_source = "FIFO_BATCH"
        elif int(allocation["allocated_quantity"]) > 0:
            purchase_cost_cents = int(allocation["total_cost_cents"])
            cost_source = "PARTIAL_BATCH"
        else:
            purchase_cost_cents = 0
            cost_source = "MISSING"
        session.execute(
            """
            UPDATE orders
            SET purchase_cost_cents = ?, cost_source = ?,
                cost_calculated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (purchase_cost_cents, cost_source, now, now, order_id),
        )
        financial = _profit_fields(order, purchase_cost_cents, cost_source)
        if available_for_order < quantity:
            inventory_status = "shortage"
            movement_type = "SALE_1C_SHORTAGE"
        elif available_after == 0:
            inventory_status = "sold_out"
            movement_type = "SALE_1C"
        else:
            inventory_status = "written_off"
            movement_type = "SALE_1C"
        session.execute(
            """
            INSERT INTO inventory_movements(
                id, variant_id, order_id, quantity_delta, balance_after, movement_type,
                note, actor_user_id, actor_name, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex[:20],
                variant["id"],
                order_id,
                -deducted,
                balance_after,
                movement_type,
                f"Списання після підтвердження 1С; ТТН: {order.get('ttn') or '-'}",
                int(actor_user_id or 0),
                actor_name.strip()[:200],
                now,
            ),
        )
        details = {
            "inventory_status": inventory_status,
            "variant_id": variant["id"],
            "requested_quantity": quantity,
            "deducted_quantity": deducted,
            "balance_after": balance_after,
            "in_transit_after": in_transit_after,
            "available_after": available_after,
            "purchase_cost_cents": purchase_cost_cents,
            "cost_source": cost_source,
            "transport_modes": allocation["transport_modes"],
        }
        _event(
            session,
            order_id,
            "RECORDED_IN_1C",
            actor_user_id,
            actor_name,
            details,
        )
        result = {
            "ok": True,
            "already_recorded": False,
            "inventory_status": inventory_status,
            "variant_id": variant["id"],
            "product_name": variant["product_name"],
            "color_code": variant["color_code"],
            "size_code": variant["size_code"],
            "quantity": quantity,
            "deducted_quantity": deducted,
            "balance_after": balance_after,
            "in_transit_after": in_transit_after,
            "available_after": available_after,
            "ttn": order.get("ttn") or "",
            "transport_modes": allocation["transport_modes"],
            **financial,
        }
        warnings: list[str] = []
        if inventory_status == "sold_out":
            warnings.append("Товар закінчився на складі")
        elif inventory_status == "shortage":
            warnings.append(
                f"Вільно було лише {available_for_order} шт., потрібно списати {quantity} шт."
            )
        if not financial["cost_ready"]:
            warnings.append(
                "Не вистачає партій із собівартістю — прибуток ще не пораховано"
            )
        if int(order.get("sale_price_cents") or 0) <= 0:
            warnings.append("Не вказана ціна продажу — прибуток ще не пораховано")
        if warnings:
            result["warning"] = ". ".join(warnings)
        return result


def edit_order_by_ttn(
    ttn: str,
    changes: dict[str, str],
    actor_user_id: int,
    actor_name: str = "",
) -> dict[str, Any] | None:
    allowed_money = {
        "price": "sale_price_cents",
        "cost": "purchase_cost_cents",
        "delivery": "delivery_cost_cents",
        "fee": "platform_fee_cents",
        "other": "other_cost_cents",
    }
    order = get_order_by_ttn(ttn)
    if not order:
        return None
    updates: list[str] = []
    values: list[Any] = []
    details: dict[str, Any] = {}
    if changes.get("product"):
        product = resolve_product(changes["product"], create=True)
        updates.extend(["product_id = ?", "raw_product_name = ?"])
        values.extend([product["id"] if product else None, changes["product"]])
        details["product"] = changes["product"]
    for key, column in allowed_money.items():
        if key in changes:
            updates.append(f"{column} = ?")
            cents = money_to_cents(changes[key])
            values.append(cents)
            details[key] = changes[key]
            if key == "cost":
                updates.extend(["cost_source = ?", "cost_calculated_at = ?"])
                values.extend(["MANUAL" if cents > 0 else "MISSING", _now()])
    if "quantity" in changes:
        updates.append("quantity = ?")
        values.append(max(1, int(changes["quantity"])))
        details["quantity"] = changes["quantity"]
    if "olx" in changes:
        updates.append("olx_order_id = ?")
        values.append(changes["olx"][:100])
        details["olx"] = changes["olx"]
    if not updates:
        return order
    with _LOCK, _transaction() as session:
        values.extend([_now(), order["id"]])
        session.execute(
            f"UPDATE orders SET {', '.join(updates)}, updated_at = ? WHERE id = ?",
            tuple(values),
        )
        _event(session, order["id"], "ORDER_EDITED", actor_user_id, actor_name, details)
    return get_order(order["id"])


def claim_notification(
    order_id: str, notification_type: str, stale_minutes: int = 10
) -> bool:
    now = _now()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)).isoformat(
        timespec="seconds"
    )
    with _LOCK, _transaction() as session:
        cursor = session.execute(
            """
            INSERT INTO order_notifications(
                order_id, notification_type, status, claimed_at, updated_at
            ) VALUES (?, ?, 'CLAIMED', ?, ?)
            ON CONFLICT(order_id, notification_type) DO UPDATE SET
                status = 'CLAIMED',
                claimed_at = excluded.claimed_at,
                updated_at = excluded.updated_at
            WHERE order_notifications.status <> 'SENT'
              AND (
                  order_notifications.status <> 'CLAIMED'
                  OR order_notifications.claimed_at < ?
              )
            """,
            (order_id, notification_type, now, now, cutoff),
        )
        return cursor.rowcount == 1


def get_notification(
    order_id: str, notification_type: str
) -> dict[str, Any] | None:
    """Return the durable Telegram-delivery state for an order card."""

    with _LOCK, _transaction() as session:
        return session.one(
            """
            SELECT order_id, notification_type, status, telegram_message_id,
                   claimed_at, updated_at
            FROM order_notifications
            WHERE order_id = ? AND notification_type = ?
            """,
            (order_id, notification_type),
        )


def finish_notification(
    order_id: str,
    notification_type: str,
    success: bool,
    telegram_message_id: int = 0,
) -> None:
    with _LOCK, _transaction() as session:
        session.execute(
            """
            UPDATE order_notifications SET status = ?, telegram_message_id = ?, updated_at = ?
            WHERE order_id = ? AND notification_type = ?
            """,
            (
                "SENT" if success else "FAILED",
                int(telegram_message_id or 0),
                _now(),
                order_id,
                notification_type,
            ),
        )


def get_monthly_turnover(month: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month):
        raise ValueError("Month must use YYYY-MM format")
    with _LOCK, _transaction() as session:
        return session.one("SELECT * FROM monthly_turnover WHERE month = ?", (month,))


def current_month_label() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m")


def clear_current_month_analytics(
    actor_user_id: int,
    actor_name: str = "",
) -> dict[str, Any]:
    """Hide analytics recorded before now in the current month only.

    Operational orders and inventory history remain intact.  The live analytics
    starts counting again from this reset point.
    """

    month = current_month_label()
    cleared_at = _now()
    with _LOCK, _transaction() as session:
        session.execute(
            """
            INSERT INTO monthly_analytics_resets(
                month, cleared_at, cleared_by, cleared_by_name
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(month) DO UPDATE SET
                cleared_at = excluded.cleared_at,
                cleared_by = excluded.cleared_by,
                cleared_by_name = excluded.cleared_by_name
            """,
            (
                month,
                cleared_at,
                int(actor_user_id or 0),
                actor_name.strip()[:200],
            ),
        )
        session.execute("DELETE FROM monthly_turnover WHERE month = ?", (month,))
    return {"month": month, "cleared_at": cleared_at}


def get_sales_stats(period: str = "month") -> dict[str, Any]:
    """Return live sales and refusal totals for a month or all time."""

    period = (period or "month").strip().lower()
    start: str | None = None
    end: str | None = None
    if period != "all":
        if period == "month":
            local_now = datetime.now(ZoneInfo(TIMEZONE))
            year, month = local_now.year, local_now.month
        elif re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", period):
            year, month = map(int, period.split("-"))
        else:
            raise ValueError("Period must be all, month, or YYYY-MM")
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        start = (
            datetime(year, month, 1, tzinfo=ZoneInfo(TIMEZONE))
            .astimezone(timezone.utc)
            .isoformat()
        )
        end = (
            datetime(next_year, next_month, 1, tzinfo=ZoneInfo(TIMEZONE))
            .astimezone(timezone.utc)
            .isoformat()
        )
        label = f"{year:04d}-{month:02d}"
    else:
        label = "all"

    sales_where = (
        "sold_at IS NOT NULL AND status NOT IN ('REFUSED', 'RETURNED', 'CANCELLED')"
    )
    refusal_where = "refused_at IS NOT NULL"
    backend = database_backend(BUSINESS_DATABASE_URL)
    sold_month = (
        "TO_CHAR(sold_at AT TIME ZONE 'Europe/Kyiv', 'YYYY-MM')"
        if backend == "postgresql"
        else "substr(sold_at, 1, 7)"
    )
    refused_month = (
        "TO_CHAR(refused_at AT TIME ZONE 'Europe/Kyiv', 'YYYY-MM')"
        if backend == "postgresql"
        else "substr(refused_at, 1, 7)"
    )
    sales_where += (
        " AND NOT EXISTS (SELECT 1 FROM monthly_analytics_resets r "
        f"WHERE r.month = {sold_month} AND sold_at < r.cleared_at)"
    )
    refusal_where += (
        " AND NOT EXISTS (SELECT 1 FROM monthly_analytics_resets r "
        f"WHERE r.month = {refused_month} AND refused_at < r.cleared_at)"
    )
    params: tuple[Any, ...] = ()
    if start and end:
        sales_where += " AND sold_at >= ? AND sold_at < ?"
        refusal_where += " AND refused_at >= ? AND refused_at < ?"
        params = (start, end)

    with _LOCK, _transaction() as session:
        sales = (
            session.one(
                f"""
            SELECT COUNT(*) AS sales_count,
                   COALESCE(SUM(quantity), 0) AS items_count,
                   COALESCE(SUM(sale_price_cents), 0) AS turnover_cents,
                   COALESCE(SUM(CASE
                       WHEN cost_source IN ('FIFO_BATCH', 'MANUAL')
                            AND sale_price_cents > 0
                       THEN purchase_cost_cents + delivery_cost_cents +
                            platform_fee_cents + other_cost_cents
                       ELSE 0 END
                   ), 0) AS expenses_cents,
                   COALESCE(SUM(CASE
                       WHEN cost_source IN ('FIFO_BATCH', 'MANUAL')
                            AND sale_price_cents > 0
                       THEN sale_price_cents - purchase_cost_cents -
                            delivery_cost_cents - platform_fee_cents - other_cost_cents
                       ELSE 0 END
                   ), 0) AS profit_cents,
                   COALESCE(SUM(CASE
                       WHEN cost_source NOT IN ('FIFO_BATCH', 'MANUAL')
                            OR sale_price_cents <= 0
                       THEN 1 ELSE 0 END
                   ), 0) AS cost_pending_count
            FROM orders WHERE {sales_where}
            """,
                params,
            )
            or {}
        )
        refusals = (
            session.one(
                f"SELECT COUNT(*) AS refusals_count FROM orders WHERE {refusal_where}",
                params,
            )
            or {}
        )

    sales_count = int(sales.get("sales_count") or 0)
    refusals_count = int(refusals.get("refusals_count") or 0)
    decisions_count = sales_count + refusals_count
    return {
        "period": label,
        "sales_count": sales_count,
        "items_count": int(sales.get("items_count") or 0),
        "turnover_cents": int(sales.get("turnover_cents") or 0),
        "expenses_cents": int(sales.get("expenses_cents") or 0),
        "profit_cents": int(sales.get("profit_cents") or 0),
        "cost_pending_count": int(sales.get("cost_pending_count") or 0),
        "refusals_count": refusals_count,
        "refusal_rate": (refusals_count * 100 / decisions_count)
        if decisions_count
        else 0.0,
    }


def business_health() -> dict[str, int]:
    with _LOCK, _transaction() as session:
        orders = session.one("SELECT COUNT(*) AS count FROM orders") or {"count": 0}
        products = session.one(
            "SELECT COUNT(*) AS count FROM products WHERE active = 1"
        ) or {"count": 0}
        pending = session.one(
            "SELECT COUNT(*) AS count FROM order_submissions WHERE status IN ('PENDING', 'PROCESSING')"
        ) or {"count": 0}
    return {
        "orders": int(orders["count"] or 0),
        "products": int(products["count"] or 0),
        "pending": int(pending["count"] or 0),
    }


def upsert_monthly_turnover(
    month: str,
    turnover: Any,
    *,
    orders_count: int = 0,
    currency: str = "UAH",
    calculated_at: str | None = None,
    source: str = "external-analytics",
    note: str = "",
) -> None:
    """Convenience API for the external analytics program and tests."""

    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month):
        raise ValueError("Month must use YYYY-MM format")
    now = _now()
    with _LOCK, _transaction() as session:
        session.execute(
            """
            INSERT INTO monthly_turnover(
                month, turnover_cents, currency, orders_count, calculated_at,
                source, note, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(month) DO UPDATE SET
                turnover_cents = ?, currency = ?, orders_count = ?,
                calculated_at = ?, source = ?, note = ?, updated_at = ?
            """,
            (
                month,
                money_to_cents(turnover),
                currency,
                max(0, int(orders_count)),
                calculated_at or now,
                source,
                note,
                now,
                money_to_cents(turnover),
                currency,
                max(0, int(orders_count)),
                calculated_at or now,
                source,
                note,
                now,
            ),
        )


def export_1c_csv(days: int = 365) -> bytes:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 3650)))
    ).isoformat()
    with _LOCK, _transaction() as session:
        rows = session.all(
            """
            SELECT o.*, p.canonical_name AS product_name
            FROM orders o LEFT JOIN products p ON p.id = o.product_id
            WHERE o.created_at >= ? ORDER BY o.created_at
            """,
            (cutoff,),
        )
    output = io.StringIO(newline="")
    columns = [
        "ttn",
        "olx_order_id",
        "sale_type",
        "carrier",
        "product_name",
        "quantity",
        "sale_price_uah",
        "purchase_cost_uah",
        "delivery_cost_uah",
        "platform_fee_uah",
        "other_cost_uah",
        "status",
        "shipped_at",
        "sold_at",
        "refused_at",
        "onec_recorded_at",
    ]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "ttn": row.get("ttn", ""),
                "olx_order_id": row.get("olx_order_id", ""),
                "sale_type": SALE_TYPE_LABELS.get(
                    row.get("sale_type", ""), row.get("sale_type", "")
                ),
                "carrier": row.get("carrier", ""),
                "product_name": row.get("product_name")
                or row.get("raw_product_name", ""),
                "quantity": row.get("quantity", 1),
                "sale_price_uah": format_money(row.get("sale_price_cents")),
                "purchase_cost_uah": format_money(row.get("purchase_cost_cents")),
                "delivery_cost_uah": format_money(row.get("delivery_cost_cents")),
                "platform_fee_uah": format_money(row.get("platform_fee_cents")),
                "other_cost_uah": format_money(row.get("other_cost_cents")),
                "status": row.get("status", ""),
                "shipped_at": row.get("shipped_at") or "",
                "sold_at": row.get("sold_at") or "",
                "refused_at": row.get("refused_at") or "",
                "onec_recorded_at": row.get("onec_recorded_at") or "",
            }
        )
    return ("\ufeff" + output.getvalue()).encode("utf-8")
