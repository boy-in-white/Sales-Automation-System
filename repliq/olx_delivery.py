"""OLX delivery signals and private alerts; no order confirmation or payment claims."""

import copy
import hashlib
import json
import re
import time

from buyer_identity import topic_title
from catalog_admin import admin_owner
from customer_phrasing import canonical, platform_notice, cod_mentioned
from shop_facts import shop_day, shipping_reply


def ukrposhta(text):
    return bool(re.search(r'\b(?:укр[ .-]*(?:пошт|почт)\w*|ukr[ .-]*(?:poshta|pochta))\b', canonical(text)))


def order_report(text):
    text = canonical(text)
    if re.search(r'(?:у|в|от)\s+(?:іншого|другого)\b|іншому\s+продавц|другому\s+продавц|не\s+у\s+вас|не\s+в\s+вас', text):
        return False
    for match in re.finditer(r'\b(?:оформив|оформила|оформлено|оформлен(?:о|ий)?|замовив|замовила|заказал|заказала)\b|\b(?:підтвердіть|подтвердите)\b', text):
        if not re.search(r'\b(?:не|ще\s+не|еще\s+не|так\s+і\s+не)\s*$', text[max(0, match.start()-25):match.start()]):
            return True
    return False


def olx_mentioned(text):
    return bool(re.search(r'\b(?:olx|олх|олікс)\b', canonical(text)))


def acknowledgment(shop, now=None):
    return 'Дякую за покупку! ' + shipping_reply(shop, now)


def cancellation_request(text):
    text = canonical(text)
    return bool(re.search(r'скасу\w*.{0,15}(?:замов|достав)|отмен\w*.{0,15}(?:заказ|достав)|'
                          r'передум\w*\s+(?:замовляти|заказывать)|^(?:передумав|передумала|не\s+треба|не\s+потрібно|не\s+нужно)[.! ]*$', text))


def delivery_plan(catalog, raw, saved, now=None):
    text = canonical(raw)
    mentioned = ukrposhta(text)
    current_olx = saved.get('checkout_method') == 'olx'
    cancelling = current_olx and cancellation_request(text)
    report = order_report(text) and (current_olx or olx_mentioned(text) or mentioned)
    confirmation = bool(re.search(r'(?:підтвердіть|подтвердите).{0,25}(?:замов|заказ|купівл|покуп)|(?:замов|заказ).{0,20}(?:вже\s+)?(?:підтверджен|подтвержден)', text))
    if not mentioned and not report and not (current_olx and confirmation) and not cancelling:
        return None
    context = copy.deepcopy(saved)
    if cancelling:
        if saved.get('olx_notice_received') or saved.get('olx_delivery_reported'):
            return {'text': 'Зрозуміло. Скасування вже оформленої OLX Доставки потрібно перевірити в OLX — повідомлю власника.',
                    'context': context, 'on_send': {'olx_cancel_requested': True}, 'guard': {}, 'notes': []}
        return {'text': 'Добре, тоді поки не оформлюємо OLX Доставку. Якщо захочете повернутися до вибору — пишіть.',
                'context': context, 'on_send': {'checkout_method': None, 'checkout_stage': None, 'checkout_offered': False}, 'guard': {}, 'notes': []}
    rejected = mentioned and bool(re.search(r'не\s+(?:хочу\s+)?укр|укр\w*\s+не\s+(?:хочу|потріб|надо)', text))
    if mentioned and not rejected:
        context['olx_carrier'] = 'ukrposhta'
    elif rejected:
        context.pop('olx_carrier', None)
        from carrier_payment import enabled
        cod = 'післяплатою з оплатою доставки за ТТН' if enabled(catalog.shop) else 'післяплатою з передоплатою 100 грн'
        return {'text': 'Добре, можна обрати Нову пошту. Оформляємо OLX Доставкою чи ' + cod + '?',
                'context': context, 'on_send': {}, 'guard': {}, 'notes': []}
    if report or confirmation:
        body = acknowledgment(catalog.shop, now)
        body += ' Замовлення перевіримо в OLX.'
        sent = {'olx_delivery_reported': True, 'olx_thanked_at': time.time()}
        if not saved.get('invoice_issued'):
            sent.update(checkout_method='olx', checkout_stage='olx_waiting')
        return {'text': body, 'context': context, 'on_send': sent,
                'guard': {'shipping_day': str(shop_day(catalog.shop, now))}, 'notes': []}
    sent = {}
    if (not saved.get('invoice_issued') and saved.get('checkout_offered') and not cod_mentioned(text) and
            re.search(r'давай|обираю|оформлю|олх|olx|хочу', text)):
        sent.update(checkout_method='olx', checkout_stage='olx_waiting')
    return {'text': 'Так, Укрпоштою можна замовити — лише через OLX Доставку з оплатою в самому OLX. '
                    'Під час оформлення оберіть Укрпошту. Якщо вона недоступна в оголошенні, напишіть — уточнимо.',
            'context': context, 'on_send': sent, 'guard': {}, 'notes': []}


def queue_alert(registry, config, account, thread, buyer, advert, context, messages, product, name=''):
    """Only the owner's private outbox is touched. Never change conversation mode.

    The API returns these templates as plain received text without an authenticated
    system-origin flag or order ID. An alert therefore asks the owner to check OLX.
    """
    owner = admin_owner(config)
    if not owner or not registry:
        return 0
    incoming = [m for m in messages if m.get('type') == 'received']
    notices = [m for m in incoming if platform_notice(m.get('text', ''))]
    combined = '\n'.join(m.get('text', '') for m in incoming if not platform_notice(m.get('text', '')))
    reported = order_report(combined) and (context.get('checkout_method') == 'olx' or olx_mentioned(combined) or ukrposhta(combined))
    sources = [('notice', m['_id']) for m in notices]
    cancelling = context.get('checkout_method') == 'olx' and cancellation_request(combined)
    if cancelling and (context.get('olx_notice_received') or context.get('olx_delivery_reported')):
        sources.append(('cancel', str(context.get('conversation_generation', 0))))
    if not notices and reported:
        sources.append(('reported', str(context.get('conversation_generation', 0))))
    queued = 0
    for source, marker in sources:
        event = hashlib.sha256(json.dumps([account, thread, buyer, advert, source, marker]).encode()).hexdigest()
        body = '🛍 OLX Доставка · перевір продаж\n' + topic_title(name, buyer, product) + '\n\n'
        body += ('Клієнт просить скасувати вже оформлену OLX Доставку. ' if source == 'cancel' else
                 'У чаті з’явилося повідомлення «У вас придбали товар з OLX Доставкою». ' if source == 'notice' else
                 'Клієнт повідомив, що оформив замовлення, або просить його підтвердити. ')
        body += 'Відкрий «Мої продажі» в OLX і перевір замовлення.\n'
        body += ('У чаті обговорювали Укрпошту; остаточного перевізника перевір у замовленні OLX.\n' if context.get('olx_carrier') == 'ukrposhta' or ukrposhta(combined) else
                 'Перевізника й накладну перевір у замовленні OLX.\n')
        if context.get('size') or context.get('color'):
            from catalog import COLORS
            body += 'Вибір у чаті: ' + ' · '.join(str(v) for v in (context.get('size'), COLORS.get(context.get('color'), context.get('color'))) if v) + '.\n'
        body += '\nОголошення: %s\nЧат OLX: %s\nАкаунт: %s\n\nКлієнтові далі відповідає auto. Оплату цим повідомленням не підтверджено.' % (advert, thread, account)
        registry.tell(owner, 'olx-delivery:' + event, body,
                      {'inline_keyboard': [[{'text': 'Відкрити OLX', 'url': 'https://www.olx.ua/d/myaccount/'}]]})
        queued += 1
    return queued
