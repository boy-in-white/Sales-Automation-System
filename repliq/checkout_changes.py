"""Quantity amendments to issued orders, without issuing duplicate payment requests."""

import copy
import re
from collections import Counter

from customer_language import requested_quantity, requested_size, requested_color, reports_payment
from inventory import StockUnavailable, stock_signature, stock_rows
from order_summary import order_items, snapshot, summary
from shop_facts import stock_groups, total_price, money, choice_text
from shopping_cart import mentions


def quantity_change(catalog, inventory, raw, context, has_attachments=False):
    """Return (body, human, guards), mutating only a copied conversation context."""
    pending = context.get("quantity_change")
    amount = requested_quantity(raw)
    if amount is None and not pending:
        return None
    if amount is not None and not (re.search(r"хочу|беру|візьм|возьм|давай|додай|добав|зроб|замість|вместо", raw) or
                                   re.fullmatch(r"\d{1,2}\s*(?:шт\.?|штук\w*|пар[аиу]?)[.! ]*", raw)):
        return None
    items = copy.deepcopy(order_items(catalog, context))
    if amount is not None:
        if not 1 <= amount <= 20:
            return ("Скільки речей потрібно загалом? Напишіть ціле число від 1 до 20; для більшого замовлення узгодимо кількість особисто.", False, {})
        refs = {ref[2] for ref in mentions(catalog, raw)}
        size, color = requested_size(raw), requested_color(raw)
        line = re.search(r"(?:позиці[яюї]|позиц[ияю]|номер|№)\s*(\d{1,2})", raw)
        targets = [i for i, item in enumerate(items) if (not refs or item["product"] in refs)
                   and (not size or item.get("size") == size) and (not color or item.get("color") == color)
                   and (not line or i == int(line[1]) - 1)]
        mode = "add" if re.search(r"додай|добав|\bще\b|\bеще\b|\bещё\b", raw) and not re.search(r"загалом|всього|всего|усього", raw) else "set"
        if len(targets) != 1:
            context["quantity_change"] = {"amount": amount, "mode": mode, "phase": "target"}
            options = "\n".join("%s. %s" % (i + 1, choice_text(catalog.products[item["product"]], item.get("size"), item.get("color"), item["quantity"])) for i, item in enumerate(items) if item["product"] in catalog.products)
            return ("Для якої речі змінити кількість? Напишіть назву, розмір і колір або номер позиції:\n\n" + options, False, {})
        pending = {"line": targets[0], "amount": amount, "mode": mode, "phase": "apply"}
    elif pending.get("phase") == "target":
        refs = {ref[2] for ref in mentions(catalog, raw)}
        size, color = requested_size(raw), requested_color(raw)
        line = re.search(r"(?:позиці[яюї]|позиц[ияю]|номер|№)\s*(\d{1,2})", raw)
        if not (refs or size or color or line):
            return None
        targets = [i for i, item in enumerate(items) if (not refs or item["product"] in refs)
                   and (not size or item.get("size") == size) and (not color or item.get("color") == color)
                   and (not line or i == int(line[1]) - 1)]
        if len(targets) != 1:
            return ("Уточніть, будь ласка, номер позиції, для якої змінюємо кількість. Решту речей залишу як є.", False, {})
        pending = dict(pending, line=targets[0], phase="apply")
    if not pending or "line" not in pending or not 0 <= pending["line"] < len(items):
        return None
    target = items[pending['line']]
    new_amount = target['quantity'] + pending['amount'] if pending['mode'] == 'add' else pending['amount']
    if new_amount == target['quantity'] and not has_attachments and not reports_payment(raw):
        context.pop('quantity_change', None)
        return ('Так, у замовленні вже записано %s шт. цієї позиції.' % new_amount, False, {})
    # A payment report/proof can refer to the old amount; the owner must reconcile it.
    if reports_payment(raw):
        context["payment_reported"] = True
    if has_attachments:
        context["unclassified_attachment"] = True
    if context.get("receipt_submitted") or context.get("payment_reported") or context.get("payment_verified"):
        context["quantity_change"] = pending
        return ("Можемо змінити кількість. Спершу звіримо вже надіслану оплату або вкладення й нову суму особисто. Повторно нічого не переказуйте.", True, {})
    if not 1 <= new_amount <= 20:
        context.pop("quantity_change", None)
        return ("Уточнимо потрібну кількість особисто, щоб правильно зібрати велике замовлення.", True, {})
    target["quantity"] = new_amount
    demands = Counter()
    for item in items:
        demands[item["product"], item["size"], item["color"]] += item["quantity"]
    stocks, signatures = {}, {}
    try:
        for (key, size, color), demand in demands.items():
            product = catalog.products.get(key)
            if not product:
                return ("Уточнимо актуальний товар перед зміною замовлення.", True, {})
            if key not in stocks:
                rows = stock_rows(inventory, product)
                stocks[key], signatures[key] = stock_groups(rows), stock_signature(rows)
            available = stocks[key].get((size, color), 0)
            if available < demand:
                context.pop("quantity_change", None)
                return ("Перевірив наявність: для цієї позиції зараз доступно %s шт. Попереднє замовлення збережено. Залишаємо його чи оберемо інший варіант?" % available, False, {"stocks": signatures})
        from size_fulfillment import allocate
        items = allocate(catalog, inventory, items)
    except StockUnavailable:
        return ("Уточнимо наявність перед зміною кількості. Попередню суму не змінюю, доки не перевіримо товари.", True, {})
    if any(item.get("unit_price") is None for item in items):
        return ("Спочатку звіримо погоджені ціни й нову кількість особисто. Повторно нічого не переказуйте.", True, {})
    total = money(total_price([item["unit_price"] for item in items], items))
    context.update(invoice_items=items, invoice_total=total, customer_confirmation=None, confirmation_sent=None,
                   confirmation_attempts=0, awaiting="shipping_details")
    context.pop("quantity_change", None)
    if context.get("cart"):
        context["cart"][pending["line"]]["quantity"] = new_amount
    else:
        context["quantity"] = new_amount
    if context.get("checkout_method") == "full":
        previous = list(context.get("previous_invoice_amounts", []))
        if context.get("checkout_amount") != total:
            previous.append(context.get("checkout_amount"))
        context.update(checkout_amount=total, previous_invoice_amounts=previous[-5:])
        payment = "Оновлена загальна вартість товарів — %s грн. Реквізити ті самі, що вище. Квитанцію можна надіслати сюди." % total
    else:
        payment = ("За товари при отриманні — %s грн. Доставка оплачується Новій пошті окремо за ТТН." % total
                   if context.get('checkout_delivery_prepay') else "Передоплата залишається 100 грн за замовлення, решта за товар — при отриманні.")
    return ("Добре, оновив кількість 😊\n\n" + summary(snapshot(catalog, context), shipping=False) + "\n\n" + payment, False, {"stocks": signatures})
