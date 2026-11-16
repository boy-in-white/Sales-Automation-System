"""One consistent order snapshot for the buyer and the owner's Telegram panel."""

import hashlib
import json
import copy
import uuid

from catalog import COLORS
from shop_facts import money, total_price

METHODS = {"cod": "Післяплата", "full": "Повна оплата", "olx": "OLX Доставка"}


def new_order_number():
    return 'RQ-' + uuid.uuid4().hex[:12].upper()


def restore_legacy_invoice(catalog, context):
    if (context.get("invoice_id") or context.get("checkout_amount") is None or
            context.get("checkout_stage") not in ("details", "owner")):
        return context
    restored = copy.deepcopy(context)
    items = order_items(catalog, restored)
    for item in items:
        item["unit_price"] = None  # Historical per-item prices cannot be inferred from today's catalog.
    restored.update(invoice_id=new_order_number(), invoice_items=items,
                    invoice_total=context["checkout_amount"] if context.get("checkout_method") == "full" else None,
                    invoice_issued=True)
    return restored


def order_items(catalog, context):
    if context.get("invoice_items"):
        return context["invoice_items"]
    cart = context.get("cart") or [{"product": context.get("product"), "size": context.get("size"),
                                   "color": context.get("color"), "quantity": context.get("quantity", 1)}]
    result = []
    for item in cart[:10]:
        product = catalog.products.get(item.get("product"))
        if product:
            result.append(dict(item, name=product["name"], unit_price=money(product["price_uah"]) if product.get("price_uah") is not None else None))
    return result


def snapshot(catalog, context):
    items = order_items(catalog, context)
    total = context.get("invoice_total")
    if total is None and items and all(item.get("unit_price") is not None for item in items):
        total = money(total_price([item["unit_price"] for item in items], items))
    return {"id": context.get("invoice_id", ""), "items": items, "total": total,
            "method": context.get("checkout_method"), "amount": context.get("checkout_amount"),
            "issued": bool(context.get("invoice_issued")), "shipping": context.get("shipping_details", {}),
            "receipt": bool(context.get("receipt_submitted")), "stage": context.get("checkout_stage"),
            "offered": bool(context.get("checkout_offered")), "confirmation": context.get("customer_confirmation", ""),
            "attachment_pending": bool(context.get("unclassified_attachment")),
            "previous_amounts": context.get("previous_invoice_amounts", []), "quantity_change": context.get("quantity_change"),
            "parcel": context.get("parcel", {}), "delivery_prepay": context.get('checkout_delivery_prepay') is True}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def invoice_key(order):
    value = {key: order[key] for key in ("id", "items", "total", "method", "amount")}
    if order.get('delivery_prepay'):
        value['payment_policy'] = 'carrier_delivery'
    return digest(value)


def confirmation_key(order):
    return digest([invoice_key(order), order["shipping"]])


def summary(order, shipping=True, reference=True, payment=True):
    lines = []
    if order["id"] and reference:
        lines.append("Замовлення №" + order["id"])
    for item in order["items"]:
        parts = [item["name"]]
        if item.get("size"):
            parts.append(item["size"])
        if item.get("color"):
            parts.append(COLORS.get(item["color"], item["color"]))
        parts.append("%s шт." % item["quantity"])
        lines.append("• " + ", ".join(parts))
    if order["total"] is not None:
        lines.append("Разом за товари: %s грн." % order["total"])
    if order["method"] and payment:
        lines.append("Оформлення: " + METHODS.get(order["method"], order["method"]))
    if order["amount"] is not None and payment:
        if order.get('delivery_prepay'):
            lines.append('Доставка — оплата Новій пошті за ТТН. Товар — повністю при отриманні.')
        else:
            lines.append("Передоплата: %s грн." % order["amount"] if order["method"] == "cod" else "До оплати: %s грн." % order["amount"])
    if shipping and order["shipping"]:
        lines.append("")
        for key, label in (("name", "ПІБ"), ("phone", "Телефон"), ("place", "Населений пункт"), ("branch", "Відділення / поштомат")):
            if order["shipping"].get(key):
                lines.append(label + ": " + order["shipping"][key])
        if order["shipping"].get("branch_address"):
            lines.append("Адреса НП: " + order["shipping"]["branch_address"])
    return "\n".join(lines)


def phase(order, verified=False):
    if order.get('delivery_prepay'):
        from carrier_payment import state_text
        if order.get('parcel', {}).get('status') == 'cancelled':
            return '⛔ Замовлення скасовано'
        if order.get('parcel', {}).get('status') in ('needs_review','invalid','uncertain'):
            return '⚠️ Накладна потребує перевірки'
        return state_text(order.get('parcel', {}))
    if verified:
        return "✅ Оплату позначено в Ledgerly"
    if order["stage"] == "owner":
        return "💰 Оплата — у Ledgerly" if order["receipt"] else "🙋 Потрібна допомога власника"
    if order["stage"] == "submitted":
        return "📦 Замовлення оформлено · оплату й відправлення веде Ledgerly"
    if order["issued"]:
        if not all(order["shipping"].get(key) for key in ("name", "phone", "place", "branch")):
            return "📍 Очікуємо дані відправки"
        if order["confirmation"] != confirmation_key(order):
            return "📋 Клієнт перевіряє замовлення й адресу"
        return "🧾 Очікуємо квитанцію" if not order["receipt"] else "💰 Оплата — у Ledgerly"
    if order["offered"]:
        return "🚚 Клієнт обирає спосіб оформлення"
    return "💬 Підбираємо товар"
