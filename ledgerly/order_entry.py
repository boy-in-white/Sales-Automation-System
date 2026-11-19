"""Owner-friendly manual order entry using the existing durable SQL wizard."""

import logging
import re
import uuid
from decimal import Decimal

from telebot import types

import business_db as db
from config import TG_EMPLOYEES_THREAD_ID, TG_GROUP_ID
from order_entry_db import advance, claim_publication
from submission_service import prepare_submission
from tracking_notify import build_employee_text
from ui import submission_keyboard

LOGGER = logging.getLogger(__name__)
KINDS = {'olx': 'ОЛХ', 'cod': 'Наложка', 'fop': 'ФОП', 'offline': 'Офлайн'}
PAGE_SIZE = 8


def _name(user):
    return str(getattr(user, 'full_name', None) or getattr(user, 'username', '') or '')


def start_order(bot, chat_id, user_id):
    db.save_inventory_wizard(user_id, chat_id, 'ORDER_TYPE', {'token': uuid.uuid4().hex[:16], 'revision': 0})
    render(bot, user_id)


def _variant(payload):
    product = db.get_inventory_product(payload.get('product_id', ''))
    variant = db.get_inventory_variant(payload.get('variant_id', ''))
    if not product or not variant or not variant['active'] or variant['product_id'] != product['id']:
        raise ValueError('Цей варіант товару змінився. Повернись до вибору товару.')
    return product, variant


def _summary(payload):
    product, variant = _variant(payload)
    lines = ['🧾 Перевір замовлення', '', KINDS[payload['kind']], product['canonical_name'],
             db.COLOR_LABELS[variant['color_code']] + ' · ' + variant['size_code'],
             'Кількість: %s шт.' % payload['quantity'],
             'Разом за всі товари: %s грн' % db.format_money(payload['price_cents'])]
    if payload.get('ttn'):
        lines.append('ТТН: ' + payload['ttn'])
    if variant['available'] < payload['quantity']:
        lines.extend(['', '⚠️ Доступно лише %s шт. Збереження не додасть відсутній товар на склад; помилка резерву потрапить у попередження.' % variant['available']])
    return '\n'.join(lines)


def render(bot, user_id):
    wizard = db.get_inventory_wizard(user_id)
    if not wizard or not wizard['step'].startswith('ORDER_'):
        return
    payload, step = wizard['payload'], wizard['step']
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    def button(label, action, value='x'):
        return types.InlineKeyboardButton(label, callback_data='new:%s:%s:%s:%s' % (payload['token'], payload['revision'], action, value))

    if step == 'ORDER_TYPE':
        text = '🧾 Нове замовлення\n\nОбери тип продажу.'
        keyboard.add(*(button(label, 'kind', key) for key, label in KINDS.items()))
    elif step == 'ORDER_PRODUCT':
        products = [p for p in db.list_inventory_products(limit=500) if p['variant_count']]
        page = max(0, min(int(payload.get('page', 0)), max(0, (len(products) - 1) // PAGE_SIZE)))
        text = 'Обери товар зі складу.' if products else 'На складі ще немає налаштованих товарів. Спочатку /add_product.'
        for product in products[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            keyboard.add(button('%s · %s шт.' % (product['canonical_name'][:55], product['available']), 'product', product['id']))
        pages = []
        if page:
            pages.append(button('←', 'page', str(page - 1)))
        if (page + 1) * PAGE_SIZE < len(products):
            pages.append(button('→', 'page', str(page + 1)))
        if pages:
            keyboard.add(*pages)
    elif step == 'ORDER_COLOR':
        product = db.get_inventory_product(payload['product_id'])
        if not product:
            raise ValueError('Товар більше недоступний. Створи нове замовлення через /new_order.')
        text = product['canonical_name'] + '\n\nОбери колір.'
        colors = db.get_product_variant_options(product['id'])['colors']
        keyboard.add(*(button(db.COLOR_LABELS[color], 'color', color) for color in colors))
    elif step == 'ORDER_SIZE':
        variants = [v for v in db.list_product_variants(payload['product_id']) if v['color_code'] == payload['color']]
        text = 'Обери розмір · %s.' % db.COLOR_LABELS[payload['color']]
        keyboard.add(*(button('%s · %s шт.' % (v['size_code'], v['available']), 'size', v['id']) for v in variants))
    elif step == 'ORDER_QUANTITY':
        _, variant = _variant(payload)
        text = 'Скільки штук? Доступно: %s.\nМожеш натиснути кнопку або написати кількість.' % variant['available']
        keyboard.add(*(button(str(q), 'qty', str(q)) for q in (1, 2, 3, 5)))
    elif step == 'ORDER_PRICE':
        text = 'Напиши загальну ціну продажу за %s шт. у гривнях.\nНаприклад: 1090 або 1090,50.' % payload['quantity']
    elif step == 'ORDER_TTN':
        text = 'Встав одну ТТН: Нова пошта — 14 цифр; Укрпошта — 13 цифр або міжнародний номер.'
    elif step == 'ORDER_REVIEW':
        text = _summary(payload)
        keyboard.add(button('Зберегти ✓', 'save'))
    elif step == 'ORDER_SAVED':
        saved = db.get_submission(payload['token'])
        if saved and saved['telegram_message_id'] == 0:
            keyboard.add(button('Повторити надсилання картки', 'publish'))
        keyboard.add(types.InlineKeyboardButton('Наступне замовлення', callback_data='new:start'))
        bot.send_message(wizard['chat_id'], 'Замовлення вже збережено в SQL. Перевір картку в «До відправки».', reply_markup=keyboard)
        return
    else:
        return
    if step != 'ORDER_TYPE':
        keyboard.add(button('← Назад', 'back'))
    keyboard.add(button('Скасувати', 'cancel'))
    bot.send_message(wizard['chat_id'], text, reply_markup=keyboard)


def _submission(payload, user):
    product, variant = _variant(payload)
    description = '%s %s %s' % (product['canonical_name'], db.COLOR_LABELS[variant['color_code']].lower(), variant['size_code'])
    price = db.format_money(payload['price_cents'])
    offline = payload['kind'] == 'offline'
    ttn = payload.get('ttn', '')
    ukr = bool(ttn and len(ttn) != 14)
    parsed = dict(manual_entry=True, inventory_variant_id=variant['id'], shipment_kind=KINDS[payload['kind']],
                  product_name=description, description=description, quantity=str(payload['quantity']),
                  cost=price, cost_text=price, sale_price=price, offline=offline)
    raw = '\n'.join([KINDS[payload['kind']], 'Укрпошта' if ukr else 'Нова пошта', 'Товар: ' + description,
                     'Кількість: %s' % payload['quantity'], 'Ціна: ' + price, 'ТТН: ' + ttn])
    return dict(raw_text=raw, parsed=parsed, ttns=[ttn] if ttn else [], source_user_name=_name(user))


def publish_submission(bot, submission_id, forward_text=None):
    saved = db.get_submission(submission_id)
    if not saved or not claim_publication(submission_id, TG_GROUP_ID):
        return False
    parsed = saved['parsed']
    offline = bool(parsed.get('offline'))
    ready, warning = (True, False) if offline else prepare_submission(bot, saved, saved['source_user_id'], saved['source_user_name'])
    if forward_text is None:
        forward_text = build_employee_text(client_name=saved['source_user_name'], user_id=saved['source_user_id'],
            shipment_kind=parsed['shipment_kind'], description=parsed['description'], cost_text=parsed['cost_text'],
            ttns=saved['ttns'], offline=offline, quantity=parsed.get('quantity', '1'))
    if not offline:
        forward_text += '\n\n' + ('📦 Товар зарезервовано. Натисни «Відправлено» після передачі посилки.' if not warning else '⚠️ Резерв складу потребує перевірки.')
        if not ready:
            forward_text += '\nТрекінг потребує повторної перевірки. Запис замовлення збережено.'
    try:
        sent = bot.send_message(TG_GROUP_ID, forward_text, message_thread_id=TG_EMPLOYEES_THREAD_ID,
                                reply_markup=submission_keyboard(submission_id, offline=offline))
    except Exception as error:
        # A definitive Telegram rejection is safe to retry. A timeout may have
        # delivered the card, so retain -1 and ask the owner to inspect the topic.
        if isinstance(getattr(error, 'error_code', None), int) and 400 <= error.error_code < 500:
            db.attach_submission_message(submission_id, TG_GROUP_ID, 0)
        raise
    db.attach_submission_message(submission_id, TG_GROUP_ID, sent.message_id)
    return True


def handle_callback(bot, call):
    user_id, chat_id = int(call.from_user.id), int(call.message.chat.id)
    if user_id != chat_id or getattr(call.message.chat, 'type', 'private') != 'private':
        bot.answer_callback_query(call.id, 'Доступно в особистому чаті з ботом', show_alert=True)
        return
    if call.data == 'new:start':
        bot.answer_callback_query(call.id)
        start_order(bot, chat_id, user_id)
        return
    wizard = db.get_inventory_wizard(user_id)
    parts = str(call.data).split(':')
    if len(parts) != 5 or not wizard or not wizard['step'].startswith('ORDER_'):
        bot.answer_callback_query(call.id, 'Ця кнопка вже неактуальна. /new_order', show_alert=True)
        return
    payload, step = wizard['payload'], wizard['step']
    _, token, revision, action, value = parts
    if token != payload.get('token') or revision != str(payload.get('revision')) or wizard['chat_id'] != chat_id:
        bot.answer_callback_query(call.id, 'Ця кнопка вже неактуальна', show_alert=True)
        return
    if step == 'ORDER_SAVED':
        saved = db.get_submission(token)
        if action == 'publish' and saved and saved['telegram_message_id'] == 0:
            bot.answer_callback_query(call.id, 'Надсилаю збережену картку…')
            try:
                publish_submission(bot, token)
            except Exception:
                LOGGER.exception('Saved order publication needs review: %s', token)
            render(bot, user_id)
        else:
            bot.answer_callback_query(call.id, 'Замовлення вже збережено. Не додаю повторно.')
        return
    updated, next_step, submission = dict(payload), step, None
    try:
        if action == 'cancel':
            next_step = 'ORDER_CANCELLED'
        elif action == 'back':
            next_step = {'ORDER_PRODUCT': 'ORDER_TYPE', 'ORDER_COLOR': 'ORDER_PRODUCT', 'ORDER_SIZE': 'ORDER_COLOR',
                         'ORDER_QUANTITY': 'ORDER_SIZE', 'ORDER_PRICE': 'ORDER_QUANTITY', 'ORDER_TTN': 'ORDER_PRICE',
                         'ORDER_REVIEW': 'ORDER_PRICE' if payload['kind'] == 'offline' else 'ORDER_TTN'}[step]
        elif action == 'kind' and step == 'ORDER_TYPE' and value in KINDS:
            updated = dict(token=token, revision=payload['revision'], kind=value)
            next_step = 'ORDER_PRODUCT'
        elif action == 'page' and step == 'ORDER_PRODUCT' and value.isdigit():
            updated['page'] = min(62, int(value))
        elif action == 'product' and step == 'ORDER_PRODUCT' and db.get_inventory_product(value) and db.list_product_variants(value):
            updated.update(product_id=value)
            next_step = 'ORDER_COLOR'
        elif action == 'color' and step == 'ORDER_COLOR' and value in db.get_product_variant_options(payload['product_id'])['colors']:
            updated['color'] = value
            next_step = 'ORDER_SIZE'
        elif action == 'size' and step == 'ORDER_SIZE':
            variant = db.get_inventory_variant(value)
            if not variant or not variant['active'] or variant['product_id'] != payload['product_id'] or variant['color_code'] != payload['color']:
                raise ValueError('Обери актуальний розмір.')
            updated['variant_id'] = value
            next_step = 'ORDER_QUANTITY'
        elif action == 'qty' and step == 'ORDER_QUANTITY' and value.isdigit() and 1 <= int(value) <= 999:
            updated['quantity'] = int(value)
            next_step = 'ORDER_PRICE'
        elif action == 'save' and step == 'ORDER_REVIEW':
            submission = _submission(payload, call.from_user)
            next_step = 'ORDER_SAVED'
        else:
            raise ValueError('Ця дія зараз недоступна.')
        if not advance(user_id, chat_id, token, payload['revision'], next_step, updated, submission):
            raise ValueError('Крок уже змінено. Скористайся новішим повідомленням.')
    except (ValueError, KeyError) as error:
        bot.answer_callback_query(call.id, str(error), show_alert=True)
        return
    bot.answer_callback_query(call.id, 'Збережено' if submission else 'Скасовано' if action == 'cancel' else '')
    if submission:
        try:
            publish_submission(bot, token)
            bot.send_message(chat_id, 'Готово. Картка у «До відправки».\nНатисни «Відправлено» лише після фактичної відправки.' if not submission['parsed']['offline'] else 'Готово. Відміть завершення офлайн-продажу в картці.')
        except Exception:
            LOGGER.exception('Manual order card publication needs review: %s', token)
            bot.send_message(chat_id, 'Замовлення збережено в SQL, але надсилання картки потребує перевірки. Не створюй його повторно. /new_order — наступне замовлення.')
            render(bot, user_id)
    elif action == 'cancel':
        bot.send_message(chat_id, 'Додавання скасовано. Склад і оборот не змінені.')
    else:
        render(bot, user_id)


def handle_text(bot, message):
    user_id, chat_id = int(message.from_user.id), int(message.chat.id)
    wizard = db.get_inventory_wizard(user_id)
    if not wizard or wizard['chat_id'] != chat_id or wizard['step'] in ('ORDER_SAVED', 'ORDER_CANCELLED') or not wizard['step'].startswith('ORDER_'):
        return False
    payload, step = wizard['payload'], wizard['step']
    text = (message.text or '').strip()
    updated = dict(payload)
    try:
        if step == 'ORDER_QUANTITY':
            if not re.fullmatch(r'[0-9]{1,3}', text) or not 1 <= int(text) <= 999:
                raise ValueError('Напиши цілу кількість від 1 до 999 або обери кнопкою.')
            updated['quantity'] = int(text)
            next_step = 'ORDER_PRICE'
        elif step == 'ORDER_PRICE':
            price = text.replace(',', '.')
            if not re.fullmatch(r'[0-9]{1,7}(?:\.[0-9]{1,2})?', price) or Decimal(price) <= 0:
                raise ValueError('Напиши додатну загальну ціну у гривнях. Наприклад: 1090,50.')
            updated['price_cents'] = int(Decimal(price) * 100)
            next_step = 'ORDER_REVIEW' if payload['kind'] == 'offline' else 'ORDER_TTN'
            updated.pop('ttn', None)
        elif step == 'ORDER_TTN':
            ttn = re.sub(r'\s+', '', text).upper()
            if not re.fullmatch(r'(?:[0-9]{13,14}|[A-Z]{2}[0-9]{9}[A-Z]{2})', ttn):
                raise ValueError('Потрібна одна коректна ТТН. Перевір кількість цифр і прибери зайвий текст.')
            if db.get_order_by_ttn(ttn):
                raise ValueError('Така ТТН уже є в обліку. Встав іншу.')
            updated['ttn'] = ttn
            next_step = 'ORDER_REVIEW'
        else:
            bot.send_message(chat_id, 'На цьому кроці обери варіант кнопкою нижче.')
            render(bot, user_id)
            return True
        if advance(user_id, chat_id, payload['token'], payload['revision'], next_step, updated):
            render(bot, user_id)
    except ValueError as error:
        bot.send_message(chat_id, str(error))
    return True
