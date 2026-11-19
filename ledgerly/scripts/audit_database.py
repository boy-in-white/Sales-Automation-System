#!/usr/bin/env python3
"""Read-only consistency audit for the configured Ledgerly database."""

from __future__ import annotations

import sys
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import business_db  # noqa: E402
import tracking_db  # noqa: E402
from db_runtime import close_pools  # noqa: E402


def _count(session, statement: str, params=()) -> int:
    row = session.one(statement, params) or {}
    return int(row.get("amount") or 0)


def main() -> None:
    business_db.init_business_db()
    tracking_db.init_db()
    failures: list[tuple[str, int]] = []
    warnings: list[tuple[str, int]] = []
    try:
        with business_db._LOCK, business_db._transaction() as session:
            checks = (
                (
                    'sales linked to a different product than their stock write-off',
                    '''SELECT COUNT(DISTINCT o.id) AS amount FROM orders o
                    JOIN inventory_movements m ON m.order_id=o.id
                    JOIN product_variants v ON v.id=m.variant_id
                    WHERE m.movement_type='SALE_1C' AND m.quantity_delta<0
                      AND (o.product_id IS NULL OR o.product_id<>v.product_id)
                      AND NOT EXISTS (SELECT 1 FROM repliq_parcel_items i WHERE i.ttn=o.ttn)''',
                ),
                (
                    "negative or over-reserved stock",
                    """
                    SELECT COUNT(*) AS amount FROM inventory_balances
                    WHERE on_hand < 0 OR reserved < 0 OR reserved > on_hand
                    """,
                ),
                (
                    "reservation totals that do not match stock",
                    """
                    SELECT COUNT(*) AS amount
                    FROM inventory_balances b
                    LEFT JOIN (
                        SELECT variant_id, SUM(quantity) AS quantity
                        FROM (
                            SELECT variant_id, quantity
                            FROM inventory_reservations
                            WHERE status = 'ACTIVE'
                            UNION ALL
                            SELECT variant_id, reserved_quantity AS quantity
                            FROM repliq_parcel_items
                            WHERE state = 'RESERVED' AND reserved_quantity > 0
                        ) commitments
                        GROUP BY variant_id
                    ) r ON r.variant_id = b.variant_id
                    WHERE b.reserved <> COALESCE(r.quantity, 0)
                    """,
                ),
                (
                    "active reservations without a stock balance",
                    """
                    SELECT COUNT(*) AS amount
                    FROM (
                        SELECT variant_id FROM inventory_reservations
                        WHERE status = 'ACTIVE'
                        UNION ALL
                        SELECT variant_id FROM repliq_parcel_items
                        WHERE state = 'RESERVED' AND reserved_quantity > 0
                    ) r
                    LEFT JOIN inventory_balances b ON b.variant_id = r.variant_id
                    WHERE b.variant_id IS NULL
                    """,
                ),
                (
                    "delivered orders without a sale date",
                    """
                    SELECT COUNT(*) AS amount FROM orders
                    WHERE status = 'DELIVERED' AND sold_at IS NULL
                    """,
                ),
                (
                    "refused orders without a refusal date",
                    """
                    SELECT COUNT(*) AS amount FROM orders
                    WHERE status = 'REFUSED' AND refused_at IS NULL
                    """,
                ),
            )
            for label, statement in checks:
                amount = _count(session, statement)
                if amount:
                    failures.append((label, amount))

        stale_before = int(time.time()) - 15 * 60
        with tracking_db._LOCK, tracking_db._transaction() as session:
            stale_tracking = _count(
                session,
                """
                SELECT COUNT(*) AS amount FROM shipments
                WHERE (tracking_message_id = -1 AND tracking_claimed_at < ?)
                   OR (onec_sent_at = -1 AND onec_claimed_at < ?)
                """,
                (stale_before, stale_before),
            )
            if stale_tracking:
                warnings.append(("stale Telegram delivery claims", stale_tracking))
            orphaned_cache = _count(
                session,
                """
                SELECT COUNT(*) AS amount
                FROM ukr_tracking_cache c
                LEFT JOIN shipments s ON s.ttn = c.ttn
                WHERE c.pending = 1 AND s.ttn IS NULL
                """,
            )
            if orphaned_cache:
                warnings.append(("orphaned Ukrposhta cache entries", orphaned_cache))
    finally:
        close_pools()

    if failures:
        print("Database audit: FAILED")
        for label, amount in failures:
            print(f"- {label}: {amount}")
        raise SystemExit(1)

    print("Database audit: OK")
    print("Stock, reservations, order dates and tracking state are consistent.")
    for label, amount in warnings:
        print(f"Warning: {label}: {amount}")


if __name__ == "__main__":
    main()
