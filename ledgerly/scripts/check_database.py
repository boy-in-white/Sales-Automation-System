#!/usr/bin/env python3
"""Initialize and verify the configured database without starting Telegram."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from business_db import business_health, init_business_db  # noqa: E402
from config import DATABASE_URL  # noqa: E402
from db_runtime import close_pools, redacted_database_url  # noqa: E402
from tracking_db import database_health, init_db  # noqa: E402


def main() -> None:
    try:
        init_db()
        init_business_db()
        tracking = database_health()
        business = business_health()
    finally:
        close_pools()
    print("Database connection: OK")
    print(f"URL: {redacted_database_url(DATABASE_URL)}")
    print(f"Shipments: {tracking['total']}; active: {tracking['active']}")
    print(f"Orders: {business['orders']}; products: {business['products']}")


if __name__ == "__main__":
    main()

