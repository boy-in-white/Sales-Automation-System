"""Garment facts, stock alternatives and the shop's local dispatch calendar."""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import re
from zoneinfo import ZoneInfo

from catalog import COLORS, SIZES

FULL_PREPAYMENT_REFUND = 'У разі відмови від отримання можливе повернення сплачених коштів за вирахуванням вартості доставки.'
COD_DEPOSIT_REFUND = 'У разі відмови від отримання передоплата 100 грн не повертається.'
COD_DEPOSIT_PURPOSE = 'Передоплата 100 грн потрібна, щоб покрити зворотну доставку в разі відмови від отримання. Ця сума входить у вартість товару, решту сплачуєте при отриманні.'
COD_WITHOUT_PREPAYMENT = 'Без передоплати 100 грн післяплатою не відправляємо. ' + COD_DEPOSIT_PURPOSE


def money(value):
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(amount, "f").rstrip("0").rstrip(".")


def total_price(prices, items):
    return sum((Decimal(str(price)) * item["quantity"] for price, item in zip(prices, items)), Decimal(0))


def shop_day(shop, now=None):
    zone = ZoneInfo(shop.get("timezone", "Europe/Kyiv"))
    moment = now or datetime.now(zone)
    return (moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)).date()


def shipping_reply(shop, now=None):
    if shop_day(shop, now).weekday() < 5:
        return "Відправка сьогодні протягом дня."
    return "Постараємося відправити сьогодні. Якщо не встигнемо — з понеділка."


def measurements_reply(product, sizes=None):
    rows = product.get("garment_measurements", {})
    sizes = list(dict.fromkeys(sizes or [s for s in SIZES if s in rows]))
    labels = {"length_cm": "довжина", "width_cm": "ширина", "waist_cm": "пояс", "leg_width_cm": "ширина штанини"}
    lines, missing = [], []
    for size in sizes:
        if size in product.get('excluded_sizes', []):
            lines.append(size + ': цей розмір не продаємо.')
            continue
        row = rows.get(size)
        if not row:
            missing.append(size)
            continue
        details = ["%s — %s см" % (label, format(row[key], "g")) for key, label in labels.items() if key in row]
        lines.append(size + ": " + ", ".join(details) + ".")
    if not lines and product.get("measurements"):
        return product["measurements"], False
    if missing:
        lines.append("Заміри %s ще потрібно уточнити." % ", ".join(missing))
    return "\n".join(lines), bool(missing) or not lines


def garment_reference(text):
    values = {}
    for field, label in (("width_cm", r"ширин\w*"), ("length_cm", r"довжин\w*|длин\w*")):
        found = re.search(r"(?:" + label + r")\s*[:—-]?\s*(\d{2,3}(?:[.,]\d+)?)", text)
        if found:
            values[field] = float(found[1].replace(",", "."))
    pair = re.fullmatch(r"\s*(\d{2,3})\s*[/,;\s]\s*(\d{2,3})\s*(?:см)?[.!]?", text)
    if pair and not values:
        values = {"width_cm": float(pair[1]), "length_cm": float(pair[2])}
    return values if values and all(15 <= value <= 200 for value in values.values()) else {}


def stock_groups(rows):
    grouped = {}
    for row in rows:
        key = (row["size_code"], row["color_code"])
        grouped[key] = grouped.get(key, 0) + max(0, int(row["available"]))
    return grouped


def choice_text(product, size=None, color=None, quantity=1):
    return (product["name"] + (" · " + size if size else "") +
            (" · " + COLORS.get(color, color) if color else "") + (" · %s шт." % quantity if quantity > 1 else ""))


def alternatives(product, rows, size, color, quantity=1):
    """Offer real variants. A smaller labelled garment is never claimed to be the requested size."""
    groups = stock_groups(rows)
    result = [{"product": product["key"], "size": s, "color": c, "quantity": quantity}
              for (s, c), amount in sorted(groups.items()) if amount >= quantity and s == size and c != color]
    smaller = {"M": "S", "L": "M"}.get(size) if product["kind"] == "pants" and any("suvene" in a.lower() for a in [product["name"]] + product["aliases"]) else None
    if smaller and color and groups.get((smaller, color), 0) >= quantity:
        result.append({"product": product["key"], "size": smaller, "color": color, "quantity": quantity})
    return result[:4]
