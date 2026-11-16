"""One read-only PostgreSQL connection; Ledgerly owns all inventory mutations."""

import hashlib
import json
import os
import time
from pathlib import Path

from app import AppError, read_json


class StockUnavailable(Exception):
    pass


def stock_rows(inventory, product):
    """Local sales restrictions never change the shared Ledgerly inventory."""
    excluded = set(product.get("excluded_sizes", []))
    return [row for row in inventory.rows(product) if row["size_code"] not in excluded]


def stock_signature(rows):
    values = sorted((str(r["variant_id"]), r["size_code"], r["color_code"], max(0, int(r["available"]))) for r in rows)
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def database_url(private_dir):
    # Never borrow Ledgerly's .env or print connection exceptions containing a DSN.
    value = os.environ.get("REPLIQ_DATABASE_URL")
    if not value:
        value = read_json(Path(private_dir) / "responder-settings.json").get("database_url", "")
    if value and (not isinstance(value, str) or not value.startswith(("postgresql://", "postgres://"))):
        raise AppError("У REPLIQ_DATABASE_URL потрібна адреса PostgreSQL з окремим користувачем читання.")
    return value


class Inventory:
    def __init__(self, url, clock=time.monotonic):
        self.url, self.clock = url, clock
        self.connection = None
        self.retry_at = 0.0

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _fetch(self, query, params=()):
        if not self.url:
            raise StockUnavailable("Потрібна адреса спільної SQL-бази в private/responder-settings.json.")
        if self.clock() < self.retry_at:
            raise StockUnavailable("SQL тимчасово недоступна; наявність не підтверджую.")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise StockUnavailable("Потрібна бібліотека psycopg із requirements.txt.") from None
        try:
            if self.connection is None or self.connection.closed:
                self.connection = psycopg.connect(
                    self.url, autocommit=True, connect_timeout=3, row_factory=dict_row,
                    application_name="repliq-stock-reader",
                    options="-c default_transaction_read_only=on -c statement_timeout=2000 -c lock_timeout=500",
                )
            return [dict(r) for r in self.connection.execute(query, params).fetchall()]
        except StockUnavailable:
            raise
        except (psycopg.Error, ValueError, TypeError, KeyError, OverflowError) as error:
            self.close()
            self.retry_at = self.clock() + 30
            if getattr(error, "sqlstate", None) == "42501":
                raise StockUnavailable("Потрібні права читання каталогу: виконай оновлений sql/repliq_reader.sql під адміністратором PostgreSQL.") from None
            raise StockUnavailable("SQL тимчасово недоступна або її структура змінилася. Наявність потрібно уточнити.") from None

    def catalogue(self):
        """Stable Ledgerly IDs, never receipt costs or customer/order data."""
        products = self._fetch("SELECT id,canonical_name,category,brand FROM business.products WHERE active=1 ORDER BY id LIMIT 2001")
        aliases = self._fetch("SELECT a.product_id,a.raw_alias FROM business.product_aliases a "
                              "JOIN business.products p ON p.id=a.product_id WHERE p.active=1 LIMIT 10001")
        variants = self._fetch("SELECT DISTINCT product_id,size_code FROM analytics.v_inventory_stock LIMIT 20001")
        if len(products) > 2000 or len(aliases) > 10000 or len(variants) > 20000:
            raise StockUnavailable("Каталог перевищив налаштовані межі; потрібна перевірка власника.")
        by_id = {str(p["id"]): dict(p, aliases=[], available_sizes=[]) for p in products}
        for row in aliases:
            if str(row["product_id"]) in by_id:
                by_id[str(row["product_id"])]["aliases"].append(str(row["raw_alias"]))
        for row in variants:
            if str(row["product_id"]) in by_id:
                by_id[str(row["product_id"])]["available_sizes"].append(str(row["size_code"]))
        for product in by_id.values():
            product["aliases"] = sorted(set(product["aliases"]))
            product["available_sizes"] = sorted(set(product["available_sizes"]))
        return list(by_id.values())

    def rows(self, product):
        product_id = product.get("ledgerly_product_id")
        if not product_id:
            raise StockUnavailable("Товар ще не зіставлений з Ledgerly. Вибери його у приватному чаті Repliq.")
        rows = self._fetch("SELECT product_id,product_name,variant_id,color_code,size_code,available "
                           "FROM analytics.v_inventory_stock WHERE product_id=%s ORDER BY size_code,color_code LIMIT 101", (str(product_id),))
        if len(rows) > 100 or any(str(r["product_id"]) != str(product_id) or r["available"] is None for r in rows):
            raise StockUnavailable("Неповні або неочікувані залишки SQL; потрібна перевірка власника.")
        try:
            return [dict(r, available=max(0, int(r["available"]))) for r in rows]
        except (TypeError, ValueError, OverflowError):
            raise StockUnavailable("Некоректна кількість у SQL; наявність не підтверджено.") from None


class DemoInventory:
    """Explicit demo only. Never constructed by the live worker."""
    def rows(self, product):
        return [{"product_id": product["key"], "product_name": product["name"],
                 "variant_id": product["key"] + "-" + size + "-" + color,
                 "size_code": size, "color_code": color,
                 "available": 0 if size == "XL" else 2}
                for size in ("S", "M", "L", "XL") for color in ("BLACK", "WHITE", "GRAY")]

    def close(self):
        pass
