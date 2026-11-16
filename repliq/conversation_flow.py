"""Conversation boundaries are independent of durable orders and shipment jobs."""

import copy
import re

from catalog import normalize
from customer_language import intents
from customer_contact import confirms
from customer_phrasing import greeting_only


def choice_help(text):
    return bool(re.search(r"(?:допом|помож|помог|помощ|підкаж|посовет).{0,40}(?:виб|выб)|(?:виб|выб).{0,30}(?:допом|помож|помог)|хочу\s+консультац", normalize(text)))


def fresh_selection(saved, product=None):
    # Never carry a previous buyer's body measurements, colors or payment into a new purchase.
    context = {key: copy.deepcopy(saved[key]) for key in ("_ad_product", "greeted") if key in saved}
    if product or saved.get("product"):
        context["product"] = product or saved["product"]
    context["conversation_generation"] = saved.get("conversation_generation", 0) + 1
    return context


def entry(text, saved, product=None, has_attachments=False):
    """Return a deliberate consultation turn, or let normal product/checkout logic handle it."""
    text = normalize(text)
    flags = intents(text)
    if has_attachments or flags.intersection({"complaint", "cancel", "payment"}) or re.search(r"квитанц|\bчек\b|власник|оператор|хозяин|жив[ао]й\s+человек", text):
        return None
    pure_greeting = greeting_only(text)
    explicit_new = bool(re.search(r"нов\w*\s+(?:замов|заказ)", text))
    repeat_purchase = bool(re.search(r"ще\s+(?:одн|раз).{0,15}(?:замов|куп)|ещ[её]\s+(?:одн|раз).{0,15}(?:заказ|куп)", text))
    help_requested = choice_help(text)
    finished_order = saved.get('olx_notice_received') or saved.get('olx_delivery_reported') or saved.get("invoice_issued") and (saved.get("payment_verified") or
                       saved.get("customer_confirmation") or saved.get("checkout_stage") in ("owner", "submitted"))
    cancelled_order = saved.get('parcel', {}).get('status') == 'cancelled'
    new_purchase = explicit_new or repeat_purchase and finished_order
    settled_order = saved.get('payment_verified') or saved.get('parcel', {}).get('ledgerly_status') in ('shipped','cancelled') or cancelled_order
    restart = not has_attachments and (new_purchase or pure_greeting and settled_order or help_requested and finished_order or
                                      cancelled_order and bool(flags.intersection({'order','availability','sizing'})))
    context = fresh_selection(saved, product) if restart else copy.deepcopy(saved)
    if pure_greeting and saved.get('invoice_issued') and not restart:
        return {'text': 'Добрий день! Я на зв’язку. Що бажаєте уточнити?', 'context': context,
                'on_send': {'greeted': True}, 'guard': {}, 'notes': []}
    specific_help = bool(re.search(r"розмір|размер|матеріал|материал|тканин|заміри|замер|кольор|колір|цвет", text))
    if help_requested and specific_help:
        return {"continue": True, "context": context, "guard": {"archive_order": True}} if restart else None
    if pure_greeting and (restart or not saved.get("invoice_issued")):
        return {"text": "Добрий день! 😊 Бажаєте замовити чи допомогти з вибором?", "context": context,
                "on_send": {"greeted": True, "awaiting": "consultation_choice"},
                "guard": {"archive_order": True} if restart else {}, "notes": []}
    if new_purchase and not help_requested:
        return {"text": "Звісно! Який товар, розмір і колір бажаєте цього разу?", "context": context,
                "on_send": {"awaiting": "size"}, "guard": {"archive_order": True}, "notes": []}
    if help_requested:
        context.update(awaiting="parameters", topic="sizing", size_advice_requested=True)
        return {"text": "Звісно, допоможу 🙂 Напишіть ваш зріст і вагу — підберемо розмір. Якщо цікавить щось інше про товар, також запитуйте.", "context": context,
                "on_send": {"awaiting": "parameters"}, "guard": {"archive_order": True} if restart else {}, "notes": []}
    if saved.get("awaiting") == "consultation_choice" and confirms(text):
        return {"text": "Підкажіть, який розмір і колір бажаєте? Якщо потрібна порада з розміром — допоможу підібрати.",
                "context": context, "on_send": {"awaiting": "size"}, "guard": {}, "notes": []}
    if restart:
        return {'continue': True, 'context': context, 'guard': {'archive_order': True}}
    return None
