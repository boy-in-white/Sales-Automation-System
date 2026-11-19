"""Ledgerly consumes prepared parcels and owns their Telegram and stock workflow."""

import logging
import json
from decimal import Decimal
import re
import threading
import time

from config import TG_GROUP_ID, TG_EMPLOYEES_THREAD_ID, TG_OWNER_USER_ID, REPLIQ_BRIDGE_DB, is_owner, SPECIAL_CLIENTS, refresh_shipping_accounts
from fulfillment_queue import ParcelQueue, digest
from business_db import create_submission, get_submission, attach_submission_message, record_order
from parcel_manifest import prepare
from tracking_service import process_manual_ttn
from tracking_db import update_shipment
from carrier_payment import direct_delivery, ready as carrier_ready, state_text as delivery_state_text, POLICY

LOGGER = logging.getLogger(__name__)
_RECEIVER = None


def amount(cents):
    return format(Decimal(cents) / 100, '.2f').rstrip('0').rstrip('.')


def bank_payment_text(row):
    prefix = ''
    if row.get('data', {}).get('receipt_required'):
        prefix = ('✅ Квитанцію отримано — збережена в Telegram.' if row.get('archive_urls') else
                  '⚠️ Квитанцію потрібно відкрити в OLX: автоматичне збереження не вдалося.' if row.get('archive_failed') else
                  '⏳ Квитанцію отримано, зберігаємо в Telegram.' if row.get('receipt_links') not in (None, '', '[]') else
                  '⏳ Очікуємо квитанцію від клієнта.') + '\n'
    return prefix + bank_evidence_text(row)


def bank_evidence_text(row):
    if row.get('payment_source') == 'monobank_review':
        return '⚠️ Банківська операція змінилася — перевір оплату вручну.'
    if row.get('payment_source') in ('monobank', 'monobank_pending_receipt'):
        try:
            proof = json.loads(row.get('bank_payment') or '{}')
            actual, credited = proof['actual_cents'], proof['credited_cents']
            if type(actual) is not int or type(credited) is not int or actual <= 0 or credited <= 0:
                raise ValueError('Invalid bank payment proof')
            note = '✅ mono: отримано ' + amount(actual) + ' грн.'
            if actual != credited:
                note += ' Зараховано як передоплату ' + amount(credited) + ' грн.'
            if row.get('payment_source') == 'monobank_pending_receipt':
                note += '\nПісля збереження квитанції оплату буде підтверджено автоматично.'
            return note
        except (KeyError, ValueError, TypeError):
            return '⚠️ Потрібно перевірити дані банківського підтвердження.'
    if not row['paid'] and row.get('payment_manual_lock'):
        if row.get('payment_review_revision'):
            label = ('⏳ Очікуємо повторну квитанцію від клієнта.'
                     if row.get('payment_review_receipts') == row.get('receipt_links') else
                     '🧾 Отримано нову квитанцію — перевір надходження коштів.')
            return label + '\nАвтоперевірка цього замовлення призупинена.'
        return '⏸ Позначку оплати знято вручну. Автоперевірка цього замовлення призупинена.'
    return '✅ Оплату перевірено' if row['paid'] else '⏳ Оплата ще не перевірена'


def validate(row, require_ttn=True):
    data = row['data']
    if (require_ttn and not re.fullmatch(r'\d{14}', str(row.get('ttn', '')))) or type(data.get('version')) is not int or data.get('version') not in (1, 2) or data.get('method') not in ('cod', 'full'):
        raise ValueError('Invalid imported parcel')
    carrier = data['version'] == 2
    if carrier != direct_delivery(data) or carrier and (data.get('payment_policy') != POLICY or data['method'] != 'cod' or not re.fullmatch(r'RQ-[A-F0-9]{12}', str(data.get('invoice_id', '')))):
        raise ValueError('Invalid carrier payment policy')
    if not isinstance(data.get('items'), list) or not 1 <= len(data['items']) <= 10:
        raise ValueError('Invalid manifest')
    total = 0
    for item in data['items']:
        if (not isinstance(item.get('product_id'), str) or len(item['product_id']) > 128 or
                not isinstance(item.get('name'), str) or not 1 <= len(item['name']) <= 120 or
                type(item.get('quantity')) is not int or not 1 <= item['quantity'] <= 20 or
                type(item.get('unit_price_cents')) is not int or not 0 < item['unit_price_cents'] <= 100000000 or
                not re.fullmatch(r'[A-Z]{1,12}', str(item.get('size', ''))) or not re.fullmatch(r'[A-Z]{1,16}', str(item.get('color', '')))):
            raise ValueError('Invalid item')
        if item.get('size_substituted') or item.get('requested_size'):
            sizes=('XS','S','M','L','XL','XXL')
            requested=item.get('requested_size')
            if item.get('size_substituted') is not True or requested not in sizes or item['size'] not in sizes or sizes.index(requested)-sizes.index(item['size']) != 1:
                raise ValueError('Invalid substituted size')
        total += item['quantity'] * item['unit_price_cents']
    expected_advance = 0 if carrier else 10000 if data['method']=='cod' else total
    if (total != data.get('total_cents') or type(data.get('advance_cents')) is not int or
            type(data.get('cod_cents')) is not int or data['advance_cents'] != expected_advance or
            data.get('cod_cents') != (total - expected_advance if data['method'] == 'cod' else 0) or data['cod_cents'] < 0):
        raise ValueError('Imported payment amount mismatch')
    recipient = data.get('recipient', {})
    if any(not isinstance(recipient.get(k), str) or not 1 <= len(recipient[k]) <= 200 for k in ('name', 'phone', 'place', 'branch')):
        raise ValueError('Invalid recipient')
    return data


def description(data):
    colors = {'BLACK': 'чорний', 'WHITE': 'білий', 'GRAY': 'сірий'}
    return '; '.join('%s %s %s — %s шт.' % (item['name'], ((item['requested_size'] + ' (відправити ' + item['size'] + ')') if item.get('size_substituted') else item['size']), colors.get(item['color'], item['color']), item['quantity']) for item in data['items'])


def receipt_links(row):
    from urllib.parse import urlsplit
    if row.get('data', {}).get('receipt_required'):
        return list(row.get('archive_urls') or [])[:3]
    try:
        values = json.loads(row.get('receipt_links') or '[]')
    except (ValueError, TypeError):
        return []
    if not isinstance(values, list):
        return []
    links = []
    for url in values[-3:]:
        if not isinstance(url, str) or len(url) > 2000 or any(ord(c) < 32 for c in url):
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and url not in links:
            links.append(url)
    return links


def with_receipts(text, row):
    links = receipt_links(row)
    if not links:
        return text
    added = 0
    for index, url in enumerate(links, 1):
        line = '\n\n🧾 Квитанція%s:\n%s' % (' %s' % index if len(links) > 1 else '', url)
        if len((text + line).encode('utf-16-le')) // 2 <= 3900:
            text += line
            added += 1
    if added < len(links):
        hint = '\n\n🧾 Усі квитанції — кнопками нижче.'
        if len((text + hint).encode('utf-16-le')) // 2 <= 4096:
            text += hint
    return text


def cancellation_text(row):
    state = row.get('cancel_state', '')
    if state == 'deleted':
        return '\n✅ ТТН видалено з Нової пошти.'
    if state in ('pending','checking','deleting','waiting'):
        return '\n⏳ Видалення ТТН з Нової пошти в обробці.'
    if state == 'not_needed':
        return '\nНакладну ще не було створено.'
    if state == 'blocked':
        return '\n⚠️ Автоматично видалити ТТН не вдалося. Перевір її в кабінеті Нової пошти.'
    if state == 'uncertain':
        return '\n⚠️ Результат видалення невідомий. Перевір ТТН у Новій пошті; автоматичного повтору немає.'
    return ''


def raw_text(row):
    data = validate(row)
    label = 'Наложка' if data['method'] == 'cod' else 'ФОП'
    return '\n'.join([label, 'Нова пошта', 'Замовлення №' + data['invoice_id'], 'Товар: ' + description(data),
                      'Кількість: ' + str(sum(item['quantity'] for item in data['items'])), 'Ціна: ' + amount(data['total_cents']), 'ТТН: ' + row['ttn']])


def card_text(row):
    data = validate(row)
    recipient = data['recipient']
    title = ('⛔ Скасовано' if row['nova_status'] == 'cancelled' else
             '⚠️ Змінилося замовлення — перевір ТТН' if row['nova_status'] == 'needs_review' else
             '🚚 Відправлено' if row.get('ledgerly_status') == 'shipped' else
             '📦 Очікує відправлення' if row.get('ledgerly_status') == 'confirmed' else
             '📦 До підтвердження')
    carrier = direct_delivery(data)
    if row.get('ledgerly_status') == 'shipped':
        instruction = '\n\nЗамовлення відправлено. Номер замовлення й ТТН збережені в цій картці.'
    elif row.get('ledgerly_status') == 'confirmed':
        instruction = '\n\nЗамовлення підтверджено, товар зарезервовано. «Відправлено» натискай після фактичної передачі посилки.'
    elif carrier:
        instruction = '\n\nНакладна підготовлена. Дочекайся підтвердження оплати доставки або перевір її в НП вручну. Товар зарезервується автоматично.'
    else:
        auto_deposit = data['method'] == 'cod' and data['advance_cents'] == 10000 and bool(data.get('bank_payment'))
        instruction = ('\n\nОплату підтверджено. Товар резервується автоматично. Натисни «Відправлено» після передачі посилки.' if row['paid'] else
                       '\n\nПеревір надходження вручну й підтвердь оплату кнопкою. Твоє рішення автоматично не змінюється.' if row.get('payment_manual_lock') else
                       '\n\nНакладна підготовлена. Дочекайся автоматичної перевірки mono або перевір оплату вручну. Далі товар зарезервується автоматично.' if auto_deposit else
                       '\n\nНакладна підготовлена. Перевір оплату — після цього товар зарезервується автоматично.')
    return with_receipts(title + ' · Repliq №' + data['invoice_id'] + '\n\n' + description(data) +
            '\nРазом за товари: ' + amount(data['total_cents']) + ' грн.' +
            ('\nПередоплати за товар немає.' if carrier else '\nПередоплата: ' + amount(data['advance_cents']) + ' грн.') +
            '\nЗа товар при отриманні: ' + amount(data['cod_cents']) + ' грн.' +
            '\nДоставку оплачує одержувач.\n\n' + recipient['name'] + '\n' + recipient['phone'] + '\n' +
            recipient['place'] + '\n' + recipient['branch'] + ('\n' + recipient['branch_address'] if recipient.get('branch_address') else '') + '\n\nТТН: ' + row['ttn'] +
            '\nАкаунт НП: ' + str(row['nova_profile']) + '\n' + (delivery_state_text(row) if carrier else bank_payment_text(row)) +
            (cancellation_text(row) if row['nova_status']=='cancelled' else instruction), row)


def keyboard(row, preparing=False):
    if row['nova_status'] == 'cancelled' or row.get('ledgerly_status') == 'shipped':
        from telebot import types
        return types.InlineKeyboardMarkup()
    from telebot import types
    keys = types.InlineKeyboardMarkup(row_width=2)
    if direct_delivery(row.get('data', {})):
        keys.add(types.InlineKeyboardButton('🔄 Перевірити оплату доставки', callback_data='rp:%s:%s:check' % (row['id'], row['invoice_key'][:12])))
        if not carrier_ready(row):
            keys.add(types.InlineKeyboardButton('✅ Доставку перевірено вручну', callback_data='rp:%s:%s:paid' % (row['id'], row['invoice_key'][:12])))
        elif row.get('delivery_state') == 'manual':
            keys.add(types.InlineKeyboardButton('↩️ Зняти позначку доставки', callback_data='rp:%s:%s:unpaid' % (row['id'], row['invoice_key'][:12])))
    else:
        automatic = bool(row['paid'] and row.get('payment_source') == 'monobank' and row.get('archive_urls'))
        label, action = ('❌ Оплата не підтвердилась', 'unpaid') if row['paid'] else ('✅ Оплату перевірено', 'paid')
        if not automatic:
            keys.add(types.InlineKeyboardButton(label, callback_data='rp:%s:%s:%s' % (row['id'], row['invoice_key'][:12], action)))
        same_review = row.get('payment_review_revision') and row.get('payment_review_receipts') == row.get('receipt_links')
        if not row['paid'] and not same_review:
            keys.add(types.InlineKeyboardButton('❌ Оплата не підтвердилась', callback_data='rp:%s:%s:unpaid' % (row['id'], row['invoice_key'][:12])))
    links = receipt_links(row)
    for index, url in enumerate(links[:3], 1):
        keys.add(types.InlineKeyboardButton('🧾 Квитанція' + (' %s' % index if len(links)>1 else ''), url=url))
    if not preparing:
        if carrier_ready(row):
            keys.add(types.InlineKeyboardButton('📦 Відправлено', callback_data='sub:c:' + row['submission_id']))
        else:
            keys.add(types.InlineKeyboardButton('✏️ Виправити', callback_data='sub:e:' + row['submission_id']))
        keys.add(types.InlineKeyboardButton('Скасувати', callback_data='sub:x:' + row['submission_id']))
    return keys


def review_text(row):
    data = validate(row, require_ttn=False)
    issue = row['nova_status'] in ('invalid', 'uncertain', 'needs_review')
    state = cancellation_text(row).strip() if row['nova_status']=='cancelled' else '⚠️ Потрібно перевірити створення ТТН' if issue else '📦 ТТН готується автоматично'
    if row['nova_status']=='cancelled':
        state = '⛔ Замовлення скасовано.\n' + state
    if direct_delivery(data):
        return ('📦 Замовлення №' + data['invoice_id'] + '\n\n' + description(data) +
                '\nЗа товар при отриманні: ' + amount(data['cod_cents']) + ' грн.' +
                '\nДоставка — оплата безпосередньо Новій пошті.\n\n' + state + '\n' + delivery_state_text(row))
    return with_receipts('🧾 Замовлення OLX · ' + data['invoice_id'] + '\n\n' + description(data) +
            '\nРазом: ' + amount(data['total_cents']) + ' грн.\nПередоплата: ' + amount(data['advance_cents']) +
            ' грн.\nПри отриманні за товар: ' + amount(data['cod_cents']) + ' грн.\n\n' + data['recipient']['name'] +
            '\n' + data['recipient']['place'] + ', ' + data['recipient']['branch'] + '\n\n' + state + '\n' +
            bank_payment_text(row), row)


def review_signature(row):
    return digest([row['invoice_key'], row['content_key'], row['paid'], row['nova_status'], row.get('receipt_links'), row.get('archive_urls'), row.get('archive_failed'), row['error'], row.get('cancel_state'), row.get('cancel_error'), row.get('delivery_state'), row.get('payment_source'), row.get('bank_payment'), row.get('payment_manual_lock'), row.get('payment_review_revision'), row.get('payment_review_receipts')])


class Receiver:
    def __init__(self, bot, queue, owner=TG_OWNER_USER_ID, group=TG_GROUP_ID, topic=TG_EMPLOYEES_THREAD_ID):
        self.bot, self.queue, self.owner, self.group, self.topic = bot, queue, owner, group, topic
        self.next_send = 0.0
        self.next_heartbeat = 0.0
        self.next_prepare = {}
        self.heartbeat()

    def heartbeat(self):
        if time.monotonic() >= self.next_heartbeat:
            self.queue.announce_consumer(2)
            self.next_heartbeat = time.monotonic()+15

    def deliver(self):
        self.heartbeat()
        if time.monotonic() < self.next_send:
            return False
        if self.archive_receipt():
            return True
        row = self.queue.claim_ledgerly()
        if not row:
            return self.review()
        row = self.decorate(row)
        try:
            refresh_shipping_accounts()
            data = validate(row)
            prepare(row['ttn'], data['items'])
            text = raw_text(row)
            parsed = {'shipment_kind': 'Наложка' if data['method'] == 'cod' else 'ФОП', 'description': description(data),
                      'cost_text': amount(data['total_cents']), 'cost': amount(data['total_cents']),
                      'quantity': str(sum(item['quantity'] for item in data['items'])), 'offline': False, 'olx_order_id': ''}
            create_submission(text, parsed, [row['ttn']], self.owner, 'Repliq', submission_id=row['submission_id'])
            saved = get_submission(row['submission_id'])
            if saved and saved.get('telegram_message_id'):
                self.queue.delivered(row['id'], saved['telegram_message_id'])
                self.prepare_ready(row['id'])
                return True
            if not self.queue.before_publish(row['id']):
                return False
            if row.get('review_message_id'):
                # Editing the same card is idempotent; do not publish a second payment request.
                try:
                    self.bot.edit_message_text(card_text(row), chat_id=self.group, message_id=row['review_message_id'], reply_markup=keyboard(row))
                except Exception as error:
                    if 'message is not modified' not in str(error).casefold():
                        if 'message to edit not found' in str(error).casefold() or "message can't be edited" in str(error).casefold():
                            with self.queue.db() as db:
                                db.execute("UPDATE parcels SET review_message_id=NULL WHERE id=?", (row['id'],))
                        self.queue.delivery_failed(row['id'], retry=True)
                        self.next_send = time.monotonic() + 10
                        return False
                message_id = row['review_message_id']
            else:
                message = self.bot.send_message(self.group, card_text(row), message_thread_id=self.topic, reply_markup=keyboard(row))
                message_id = message.message_id
            attach_submission_message(row['submission_id'], self.group, message_id)
            self.queue.delivered(row['id'], message_id)
            self.prepare_ready(row['id'])
            self.next_send = time.monotonic() + 3.2
            return True
        except (ValueError, KeyError, TypeError):
            self.queue.delivery_failed(row['id'])
            LOGGER.warning('Repliq import needs review: %s', row['id'])
        except Exception:
            self.queue.delivery_failed(row['id'])
            LOGGER.warning('Repliq card delivery needs review: %s', row['id'])
        return False

    def review(self):
        row = self.queue.claim_review()
        if not row:
            return False
        row = self.decorate(row)
        try:
            message = self.bot.send_message(self.group, review_text(row), message_thread_id=self.topic, reply_markup=keyboard(row, preparing=True))
            self.queue.review_result(row['id'], message.message_id, review_signature(row))
        except Exception:
            self.queue.review_result(row['id'])
            LOGGER.warning('Repliq payment card needs review: %s', row['id'])
        self.next_send = time.monotonic() + 3.2
        return True

    def refresh(self):
        self.heartbeat()
        if time.monotonic() < self.next_send:
            return
        for row in self.queue.review_updates():
            row = self.decorate(row)
            signature = review_signature(row)
            if signature == row['review_signature']:
                continue
            try:
                self.bot.edit_message_text(review_text(row), chat_id=self.group, message_id=row['review_message_id'], reply_markup=keyboard(row, preparing=True))
            except Exception as error:
                if 'message is not modified' not in str(error).casefold():
                    self.next_send = time.monotonic() + 10
                    return
            self.queue.review_result(row['id'], row['review_message_id'], signature)
            self.next_send = time.monotonic() + 3.2
            return
        for row in self.queue.notifications():
            self.prepare_ready(row['id'])
            row = self.queue.get(row['id'])
            row = self.decorate(row)
            if row['ledgerly_status'] not in ('sent', 'confirmed', 'shipped', 'cancelled') or not row['message_id'] or row['notified_revision'] == row['payment_revision']:
                continue
            try:
                self.bot.edit_message_text(card_text(row), chat_id=self.group, message_id=row['message_id'], reply_markup=keyboard(row))
            except Exception as error:
                if 'message is not modified' not in str(error).casefold():
                    return
            with self.queue.db() as db:
                db.execute('UPDATE parcels SET notified_revision=? WHERE id=?', (row['payment_revision'], row['id']))
            self.next_send = time.monotonic() + 3.2
            return

    def prepare_ready(self, row_id):
        row = self.queue.get(row_id)
        if not row or row['ledgerly_status'] not in ('sent', 'confirmed') or not carrier_ready(row):
            return
        if self.next_prepare.get(row_id, 0) > time.monotonic():
            return
        from submission_service import prepare_submission
        saved = get_submission(row['submission_id'])
        if not saved:
            return
        success, warning = prepare_submission(self.bot, saved, self.owner, 'Repliq')
        self.next_prepare[row_id] = time.monotonic() + (300 if success else 60)
        if warning:
            LOGGER.warning('Repliq inventory reservation needs review: %s', row_id)

    def decorate(self, row):
        receipts = self.queue.receipts(row['id'])
        return dict(row, archive_urls=[r['permalink'] for r in receipts if r['state']=='sent' and r['permalink']],
                    archive_failed=any(r['state'] in ('failed','uncertain') for r in receipts))

    def archive_receipt(self):
        row = self.queue.claim_receipt()
        if not row:
            return False
        try:
            # Telegram fetches the OLX attachment and retains its own copy. The
            # order card links to this private group message, not an expiring CDN URL.
            caption = '🧾 Квитанція · замовлення №' + str(row['invoice_id'])[:80] + '\nНадходження коштів перевіряється окремо через mono.'
            try:
                message = self.bot.send_photo(self.group, row['receipt_url'], caption=caption, message_thread_id=self.topic)
            except Exception as error:
                if getattr(error, 'error_code', None) != 400:
                    raise
                message = self.bot.send_document(self.group, row['receipt_url'], caption=caption, message_thread_id=self.topic)
            self.queue.receipt_result(row['id'],row['invoice_key'],row['receipt_hash'],'sent',self.group,message.message_id)
        except Exception as error:
            code = getattr(error, 'error_code', None)
            self.queue.receipt_result(row['id'],row['invoice_key'],row['receipt_hash'],
                                      'failed' if isinstance(code,int) and 400<=code<500 else 'uncertain',
                                      error='telegram_receipt_' + (str(code) if isinstance(code,int) else type(error).__name__))
            LOGGER.warning('Receipt archiving needs review for order %s', row['id'])
        self.next_send = time.monotonic() + 3.2
        return True


def callback_payment(bot, call):
    if not str(call.data).startswith('rp:'):
        return False
    receiver = _RECEIVER
    if not receiver or not is_owner(int(call.from_user.id)) or call.message.chat.id != receiver.group or getattr(call.message, 'message_thread_id', None) != receiver.topic:
        bot.answer_callback_query(call.id, 'Лише для власника в темі відправлення', show_alert=True)
        return True
    parts = str(call.data).split(':')
    row = receiver.queue.get(parts[1]) if len(parts) == 4 else None
    if not row or row['invoice_key'][:12] != parts[2] or parts[3] not in ('paid', 'unpaid', 'check') or call.message.message_id not in (row['message_id'], row.get('review_message_id')):
        bot.answer_callback_query(call.id, 'Ця картка вже неактуальна', show_alert=True)
        return True
    if parts[3] == 'check':
        if direct_delivery(row['data']) and receiver.queue.request_delivery_check(row['id'], row['invoice_key']):
            bot.answer_callback_query(call.id, 'Перевіряємо стан у Новій пошті; картка оновиться автоматично.')
        else:
            bot.answer_callback_query(call.id, 'Для цього замовлення перевір оплату вручну.', show_alert=True)
        return True
    if not receiver.queue.payment(row['id'], row['invoice_key'], call.from_user.id, parts[3] == 'paid'):
        bot.answer_callback_query(call.id, 'Замовлення потребує перевірки перед зміною позначки.', show_alert=True)
        return True
    bot.answer_callback_query(call.id, '✅ Позначено' if parts[3] == 'paid' else
                              'Клієнт отримає прохання повторно надіслати квитанцію.' if not direct_delivery(row['data']) else 'Позначку знято')
    return True


def shipment_allowed(bot, call, submission_id):
    if not _RECEIVER:
        return True
    row = _RECEIVER.queue.by_submission(submission_id)
    if not row:
        return True
    action = str(call.data).split(':', 2)[1] if ':' in str(call.data) else ''
    valid_stage = (action == 'r' and row['ledgerly_status'] == 'sent') or (
        action == 'c' and row['ledgerly_status'] in ('sent', 'confirmed')
    )
    if (not carrier_ready(row) or not valid_stage or call.message.chat.id != _RECEIVER.group or
            row['message_id'] != call.message.message_id):
        message = ('Спочатку дочекайся актуального підтвердження оплати доставки або перевір її в НП вручну.' if direct_delivery(row['data']) else 'Спочатку перевір оплату й натисни «✅ Оплату перевірено».'
                   if not row['paid'] else 'Ця кнопка вже неактуальна. Скористайся кнопкою в оновленій картці.')
        bot.answer_callback_query(call.id, message, show_alert=True)
        return False
    return True


def cancellation_allowed(bot, call, submission_id):
    row = _RECEIVER.queue.by_submission(submission_id) if _RECEIVER else None
    if not row:
        return True
    valid = (is_owner(int(call.from_user.id)) and call.message.chat.id == _RECEIVER.group and
             getattr(call.message, 'message_thread_id', None) == _RECEIVER.topic and call.message.message_id == row['message_id'] and
             row['ledgerly_status'] != 'shipped')
    if not valid:
        bot.answer_callback_query(call.id, 'Скасувати створену ботом ТТН може власник у її картці до відправлення.', show_alert=True)
    return valid


def process_imported(bot, submission):
    if not _RECEIVER:
        return None
    row = _RECEIVER.queue.by_submission(submission['id'])
    if not row:
        return None
    if not carrier_ready(row):
        return 'failed'
    data = validate(row)
    label = 'Наложка' if data['method'] == 'cod' else 'ФОП'
    record_order(ttn=row['ttn'], sale_type_label=label, carrier='NovaPoshta', product_name=description(data),
                 quantity=sum(item['quantity'] for item in data['items']), sale_price=amount(data['total_cents']),
                 source_user_id=_RECEIVER.owner, source_user_name='Repliq')
    result = process_manual_ttn(bot, row['ttn'], raw_text(row), description=description(data), shipment_kind=label,
                                cost_text=amount(data['total_cents']), owner_name=SPECIAL_CLIENTS.get(_RECEIVER.owner, {}).get('display_name'),
                                source_user_id=_RECEIVER.owner, source_user_name='Repliq', source_chat_id=_RECEIVER.group,
                                source_thread_id=_RECEIVER.topic, source_message_id=row['message_id'])
    update_shipment(row['ttn'], profile_name=row['nova_profile'])
    return 'processed' if result[0] else 'failed'


def submission_confirmed(submission_id):
    if not _RECEIVER:
        return
    row = _RECEIVER.queue.by_submission(submission_id)
    if not row:
        return
    with _RECEIVER.queue.db() as db:
        db.execute("UPDATE parcels SET ledgerly_status='confirmed',payment_revision=payment_revision+1,updated=? WHERE id=? AND ledgerly_status='sent'",
                   (time.time(), row['id']))


def submission_finished(submission_id, cancelled=False):
    if _RECEIVER:
        row = _RECEIVER.queue.by_submission(submission_id)
        if row:
            if cancelled:
                _RECEIVER.queue.cancel(row['account'], row['thread'], row['invoice_id'])
                return
            with _RECEIVER.queue.db() as db:
                db.execute('UPDATE parcels SET ledgerly_status=?,nova_status=CASE WHEN ? THEN ? ELSE nova_status END,payment_revision=payment_revision+1,updated=? WHERE id=?',
                           ('cancelled' if cancelled else 'shipped', int(cancelled), 'cancelled', time.time(), row['id']))


def start_receiver(bot, stop):
    global _RECEIVER
    if not REPLIQ_BRIDGE_DB:
        return None
    if not TG_OWNER_USER_ID or not TG_GROUP_ID or not TG_EMPLOYEES_THREAD_ID:
        raise RuntimeError('Ledgerly Repliq bridge requires owner and shipping topic')
    _RECEIVER = Receiver(bot, ParcelQueue(REPLIQ_BRIDGE_DB))
    _RECEIVER.queue.recover_receipts()
    _RECEIVER.queue.recover_ledgerly()
    def run():
        while not stop.is_set():
            try:
                _RECEIVER.deliver()
                _RECEIVER.refresh()
            except Exception:
                LOGGER.warning('Repliq bridge temporarily unavailable')
            stop.wait(2)
    thread = threading.Thread(target=run, name='ledgerly-repliq', daemon=True)
    thread.start()
    return thread
