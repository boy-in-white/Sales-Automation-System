"""Parcel preparation and the Repliq side of the private Ledgerly handoff."""

import json
import hashlib
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re
import threading
import time
from zoneinfo import ZoneInfo

from app import read_json, write_private, AppError
from fulfillment_queue import ParcelQueue, digest
from inventory import Inventory, StockUnavailable, stock_rows, database_url
from nova_shipping import NovaShipping, CarrierError
from order_summary import snapshot, invoice_key, confirmation_key
from shop_facts import stock_groups
from carrier_payment import direct_delivery, enabled as carrier_enabled, POLICY, evidence


def active_profile(settings, now=None):
    zone = ZoneInfo("Europe/Kyiv")
    moment = now or datetime.now(zone)
    day = (moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)).day
    schedule = settings.get("schedule", {})
    return schedule.get("first_half", "first") if day <= 15 else schedule.get("second_half", "second")


def settings_at(private_dir):
    settings = read_json(Path(private_dir) / "shipping-settings.json")
    if not settings.get("enabled"):
        return {}
    if not isinstance(settings.get("bridge_db"), str) or not Path(settings["bridge_db"]).expanduser().is_absolute():
        raise AppError("Перевір шлях bridge_db у private/shipping-settings.json.")
    if not isinstance(settings.get("profiles"), dict) or not settings.get("schedule"):
        raise AppError("Потрібні профілі Нової пошти й розклад у private/shipping-settings.json.")
    return settings


def cents(value):
    amount = Decimal(str(value)) * 100
    if not amount.is_finite() or amount != amount.to_integral_value() or not 0 <= amount <= 100000000:
        raise ValueError("Invalid parcel amount")
    return int(amount)


def payment_to_queue(private_dir, account, thread, order_id, key, owner, verified):
    settings = settings_at(private_dir)
    if settings:
        queue = ParcelQueue(settings["bridge_db"])
        queue.payment(digest([str(account), str(thread), order_id])[:24], key, owner, verified)


def parcel_payload(catalog, context, account, thread, buyer, advert):
    if context.get('quantity_change') or context.get('cancellation_requested'):
        return None
    order = snapshot(catalog, context)
    if context.get("shipping_directory_required"):
        from shipping_directory import details_key
        if (context.get("shipping_directory_key") != details_key(order["shipping"]) or
                not order["shipping"].get("city_ref") or not order["shipping"].get("warehouse_ref")):
            return None
    if (not context.get("fulfillment_opt_in") or not order["issued"] or not order["id"] or order["method"] not in ("cod", "full") or
            order["confirmation"] != confirmation_key(order) or any(not order["shipping"].get(k) for k in ("name", "phone", "place", "branch"))):
        return None
    items = []
    for item in order["items"]:
        product = catalog.products.get(item.get("product"))
        if (not product or not product.get("ledgerly_product_id") or not item.get("size") or not item.get("color") or
                item["size"] in product.get("excluded_sizes", []) or item.get("unit_price") is None or not 1 <= item["quantity"] <= 20):
            return None
        items.append({"product": product["key"], "product_id": str(product["ledgerly_product_id"]), "name": item["name"],
                      "size": item["size"], "color": item["color"], "quantity": item["quantity"], "unit_price_cents": cents(item["unit_price"])})
    if order["total"] is None or order["amount"] is None:
        return None
    total, advance = cents(order["total"]), cents(order["amount"])
    if not items or total <= 0 or sum(item["unit_price_cents"] * item["quantity"] for item in items) != total or advance > total:
        return None
    result = {"version": 1, "account": str(account), "thread": str(thread), "buyer": str(buyer), "advert": str(advert),
            "invoice_id": order["id"], "invoice_key": invoice_key(order), "method": order["method"],
            "total_cents": total, "advance_cents": advance, "cod_cents": total - advance if order["method"] == "cod" else 0,
            "items": items, "recipient": order["shipping"]}
    if context.get('receipt_required'):
        result['receipt_required'] = True
    if direct_delivery(order):
        if not carrier_enabled(catalog.shop):
            return None
        if order['method'] != 'cod' or advance != 0:
            return None
        result.update(version=2, payment_policy=POLICY)
    bank = context.get('bank_payment')
    if isinstance(bank, dict) and bank.get('invoice_key') == result['invoice_key'] and result['version'] == 1:
        result['bank_payment'] = dict(bank)
    return result


class Fulfillment:
    def __init__(self, private_dir, stop, catalog=None, carrier_factory=NovaShipping, inventory=None, can_create=None):
        self.private_dir, self.stop, self.catalog = Path(private_dir), stop, catalog
        self.settings = settings_at(private_dir)
        self.queue = ParcelQueue(self.settings["bridge_db"]) if self.settings else None
        self.carrier_factory = carrier_factory
        self.inventory = inventory
        self.thread = None
        self.can_create = can_create or (lambda row: True)
        self.next_delivery_scan = 0.0

    def sync(self, account, thread, buyer, advert, context, catalog):
        if not self.queue:
            return None
        payload = parcel_payload(catalog, context, account, thread, buyer, advert)
        if not payload:
            if context.get("invoice_id"):
                self.queue.hold(account, thread, context["invoice_id"])
            return None
        row = self.queue.put(payload)
        if context.get("receipt_links"):
            self.queue.attach_receipts(row["id"], context["receipt_links"])
        if context.get("payment_verified") and context.get("verified_invoice_key") == payload["invoice_key"] and row["payment_revision"] == 0:
            self.queue.payment(row["id"], payload["invoice_key"], context.get("payment_verified_by", 0), True)
        return row

    def process_one(self):
        if not self.queue:
            return False
        settings = settings_at(self.private_dir)
        selected = active_profile(settings)
        if settings.get("profiles", {}).get(selected, {}).get("nova_api_key"):
            self.queue.retry_configuration(selected)
        self.queue.reap_leases()
        allow_carrier = (self.catalog is None or carrier_enabled(self.catalog.shop)) and self.queue.consumer_ready()
        row = self.queue.claim_nova(allow_carrier=allow_carrier)
        if not row:
            return False
        try:
            settings = settings_at(self.private_dir)
            name = selected
            profile = settings.get("profiles", {}).get(name, {})
            if not profile.get("nova_api_key") or not profile.get("parcel"):
                raise CarrierError("active_profile_not_configured:" + str(name))
            if self.settings.get("require_setup") and profile.get("api_key_fingerprint") != hashlib.sha256(profile["nova_api_key"].encode()).hexdigest():
                configured = self.carrier_factory(profile).configure_sender(settings["sender_place"], settings["sender_branch"])
                latest = settings_at(self.private_dir)
                if latest.get("profiles", {}).get(name, {}).get("nova_api_key") != profile["nova_api_key"]:
                    raise CarrierError("profile_changed_during_setup")
                latest["profiles"][name] = configured
                write_private(self.private_dir / "shipping-settings.json", latest)
                profile, settings = configured, latest
            if not profile.get("sender"):
                raise CarrierError("sender_not_configured")
            if time.time() - row["created"] > 86400:
                raise CarrierError("parcel_request_expired")
            for key in ("city_ref", "counterparty_ref", "warehouse_ref", "contact_ref", "phone"):
                if not profile["sender"].get(key):
                    raise CarrierError("sender_not_configured")
            profile_key = digest(profile)
            if not self.queue.profile(row["id"], row["content_key"], name, profile_key, hashlib.sha256(profile['nova_api_key'].encode()).hexdigest()):
                return True
            if self.inventory and self.catalog:
                demands = Counter()
                for item in row["data"]["items"]:
                    demands[item["product"], item["size"], item["color"]] += item["quantity"]
                stocks = {}
                for (key, size, color), quantity in demands.items():
                    if key not in stocks:
                        stocks[key] = stock_groups(stock_rows(self.inventory, self.catalog.products[key]))
                    if stocks[key].get((size, color), 0) < quantity:
                        raise CarrierError("stock_changed_before_waybill")
            client = self.carrier_factory(profile)
            resolved = client.resolve(row["data"])
            client.document_properties(row["data"], resolved, "", "")
            if not self.can_create(row):
                self.queue.fail(row["id"], "order_no_longer_active")
                return True
            if not self.queue.before_write(row["id"], row["content_key"], "creating_contact"):
                return True
            recipient, contact = client.recipient(resolved)
            latest = settings_at(self.private_dir)
            if active_profile(latest) != name or digest(latest.get("profiles", {}).get(name, {})) != profile_key:
                self.queue.restart_before_document(row["id"])
                return True
            props = client.document_properties(row["data"], resolved, recipient, contact)
            if not self.can_create(row):
                self.queue.fail(row["id"], "order_no_longer_active")
                return True
            if not self.queue.before_write(row["id"], row["content_key"], "creating"):
                return True
            ttn, ref = client.create(props)
            self.queue.carrier_done(row["id"], row["content_key"], ttn, ref)
        except CarrierError as error:
            self.queue.fail(row["id"], error.code, error.uncertain)
            current = self.queue.get(row['id'])
            if current['cancel_state'] == 'waiting':
                self.queue.cancellation_result(row['id'], 'uncertain' if error.uncertain else 'not_needed', error.code)
        except StockUnavailable:
            self.queue.fail(row["id"], "stock_temporarily_unavailable")
        except (ValueError, KeyError, TypeError, OSError):
            current = self.queue.get(row["id"])
            unknown = current and current["nova_status"] in ("creating", "creating_contact")
            self.queue.fail(row["id"], "parcel_configuration_error", bool(unknown))
        except Exception:
            current = self.queue.get(row["id"])
            unknown = current and current["nova_status"] in ("creating", "creating_contact")
            self.queue.fail(row["id"], "parcel_processing_failed", bool(unknown))
        current = self.queue.get(row['id'])
        if current['cancel_state'] == 'waiting':
            self.queue.cancellation_result(row['id'], 'uncertain', 'creation_result_unknown_after_cancel')
        return True

    def process_cancellation(self):
        row = self.queue.claim_cancellation() if self.queue else None
        if not row:
            return False
        try:
            profile = settings_at(self.private_dir).get('profiles', {}).get(row['nova_profile'], {})
            key = profile.get('nova_api_key')
            fingerprint = hashlib.sha256(key.encode()).hexdigest() if key else ''
            matches = (fingerprint == row['nova_api_fingerprint']) if row['nova_api_fingerprint'] else digest(profile) == row['nova_profile_key']
            if not key or not matches or not row['nova_ref']:
                raise CarrierError('original_profile_or_reference_missing')
            client = self.carrier_factory(profile)
            status = client.document_status(row['ttn'])
            if status == '2':
                self.queue.cancellation_result(row['id'], 'deleted')
                return True
            if status in ('', '0', '3'):
                raise CarrierError('tracking_not_ready')
            if status != '1':
                raise CarrierError('parcel_already_in_transit')
            if not self.queue.before_delete(row['id']):
                return True
            client.delete_document(row['nova_ref'])
            self.queue.cancellation_result(row['id'], 'deleted')
        except CarrierError as error:
            current = self.queue.get(row['id'])
            reading = current['cancel_state'] == 'checking'
            retry = reading and error.code in ('carrier_unavailable','tracking_not_ready','tracking_identity_unknown','carrier_http_429','carrier_http_500','carrier_http_502','carrier_http_503','carrier_http_504') and current['cancel_attempts'] < 3
            self.queue.cancellation_result(row['id'], 'pending' if retry else 'uncertain' if error.uncertain else 'blocked', error.code)
        except Exception:
            current = self.queue.get(row['id'])
            self.queue.cancellation_result(row['id'], 'uncertain' if current['cancel_state']=='deleting' else 'blocked', 'cancellation_processing_failed')
        return True

    def process_delivery_payments(self):
        if not self.queue or time.monotonic() < self.next_delivery_scan:
            return False
        self.next_delivery_scan = time.monotonic() + 5
        jobs = self.queue.claim_delivery_checks()
        groups = {}
        settings = settings_at(self.private_dir)
        for row in jobs:
            groups.setdefault(row['nova_profile'], []).append(row)
        for name, rows in groups.items():
            profile = settings.get('profiles', {}).get(name, {})
            key = profile.get('nova_api_key', '')
            fingerprint = hashlib.sha256(key.encode()).hexdigest() if key else ''
            valid = []
            for row in rows:
                if not key or fingerprint != row['nova_api_fingerprint']:
                    self.queue.delivery_result(row, {'state': 'unknown', 'reason': 'original_profile_unavailable'})
                else:
                    valid.append(row)
            if not valid:
                continue
            try:
                documents = self.carrier_factory(profile).payment_documents(valid)
                by_number = {}
                for doc in documents:
                    number = str(doc.get('Number', '')) if isinstance(doc, dict) else ''
                    by_number.setdefault(number, []).append(doc)
                for row in valid:
                    matches = by_number.get(row['ttn'], [])
                    result = evidence(matches[0] if len(matches)==1 else None, row)
                    self.queue.delivery_result(row, result)
            except Exception:
                for row in valid:
                    self.queue.delivery_result(row, {'state': 'unknown', 'reason': 'carrier_unavailable'}, error=True)
        return bool(jobs)

    def start(self):
        if not self.queue or self.thread:
            return
        self.queue.recover_carrier()
        def run():
            own_inventory = self.inventory is None
            if own_inventory:
                self.inventory = Inventory(database_url(self.private_dir))
            try:
                while not self.stop.is_set():
                    try:
                        if not self.process_cancellation():
                            self.process_one()
                        self.process_delivery_payments()
                    except Exception:
                        pass  # Carrier jobs keep their durable state; OLX polling stays responsive.
                    self.stop.wait(2)
            finally:
                if own_inventory:
                    self.inventory.close()
        self.thread = threading.Thread(target=run, name="repliq-parcels", daemon=True)
        self.thread.start()
