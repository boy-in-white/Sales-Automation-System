"""Explicit, expiring consent for an owner-reviewed payment exception."""

import hashlib
import json
import re
import time

from customer_phrasing import canonical

TTL_SECONDS = 30 * 60
GUARD_KEY = 'payment_exception'


def selection_key(saved):
    fields = ('product', 'size', 'color', 'quantity', 'cart', 'invoice_id',
              'invoice_items', 'checkout_method', 'invoice_total')
    data = {key: saved.get(key) for key in fields}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def consent(text):
    """Questions, conditions and mixed yes/no messages never imply agreement."""
    text = canonical(text).strip()
    if re.search(r'[?]|\b(?:не|ні|нет|якщо|если|але|но|можливо|может|потім|потом)\b', text):
        return False
    if re.fullmatch(r'(?:так|да|добре|ок|окей|ok|згоден|згодна|погоджуюсь|погоджуюся|согласен|согласна|підтверджую|👍|✅)[.! ]*', text):
        return True
    return bool(re.fullmatch(
        r'(?:(?:так|да|добре|згоден|згодна)[, .!]*\s*)?'
        r'(?:оплачу|сплачу|заплачу|оплачуємо)\s+(?:за\s+)?(?:повернення|возврат)'
        r'(?:\s+(?:посилки|посылки))?[.! ]*', text))


def plan(inventory, raw, saved, *, now=None, has_attachments=False):
    text = canonical(raw)
    now = time.time() if now is None else now
    pending = bool(saved.get('return_payment_exception_pending'))
    shortage = bool(re.search(
        r'(?:нема[є]?|не\s+маю|нет|не\s+(?:вистач|вистарча|хвата)\w*|недостат\w*|бракує).{0,35}(?:грош|кошт|денег|передоплат)|'
        r'(?:грош\w*|кошт\w*|денег).{0,25}(?:нема[є]?|нет|не\s+(?:вистач|вистарча|хвата)\w*|недостат)|'
        r'(?:на\s+карт[цікеу]+).{0,25}(?:нема[є]?|не\s+маю|нет|не\s+вистач\w*|недостат)|'
        r'(?:нема[є]?|не\s+маю|нет).{0,20}(?:на\s+карт[цікеу]+)|'
        r'(?:лише|тільки|только)\s+(?:готів|налич)', text))
    if not (pending or shortage) or has_attachments or len(text) > 1500:
        return None
    if not (saved.get('checkout_offered') or saved.get('invoice_issued') or pending):
        return None
    from customer_language import intents, requested_size, requested_color, requested_quantity
    from conversation_policy import requests_owner
    if (intents(text).intersection({'cancel', 'complaint', 'shipment_status'}) or requests_owner(text)
            or requested_size(text) or requested_color(text) or requested_quantity(text)):
        return None
    if (saved.get('payment_verified') or saved.get('receipt_submitted')
            or saved.get('olx_notice_received') or saved.get('olx_delivery_reported')
            or saved.get('checkout_stage') in ('submitted', 'olx_waiting')
            or saved.get('parcel', {}).get('ttn')):
        if not shortage:
            return None
        return _reply(saved, 'Оплату або оформлення цього замовлення вже розпочато. Передам власнику ваше прохання, щоб узгодити умови без повторної оплати.', 'review', handoff=True)
    if pending and re.search(r'повернен|возврат', text) and re.search(r'[?]|якщо|если|скільки|сколько|хто|кто|\bяк\b|\bкак\b', text):
        result = _reply(saved, 'Умова така: якщо відмовитесь забирати посилку, оплачуєте її повернення. Точну вартість визначає перевізник — наперед суму не обіцяю. Погоджуєтесь на цю умову?', 'clarify')
        result['on_send']['return_payment_exception_pending'] = True
        return result
    if pending and re.search(r'не\s+(?:згод|соглас|оплачу|сплачу)|не\s+буду|^(?:ні|нет)[.! ]*$', text):
        return _reply(saved, 'Зрозуміло. Залишаються звичайні умови оплати. Можемо оформити пізніше, коли вам буде зручно.', 'declined')
    if pending:
        if not consent(text):
            return None
        offered_at = saved.get('return_payment_exception_at')
        if (saved.get('return_payment_exception_selection') != selection_key(saved)
                or not isinstance(offered_at, (int, float)) or isinstance(offered_at, bool)
                or not 0 <= now - offered_at <= TTL_SECONDS):
            return _reply(saved, 'Попередні умови потрібно перевірити ще раз. Напишіть, будь ласка, чи все ще потрібне оформлення без передоплати.', 'expired')
    try:
        count = inventory.monthly_refusals()
        if type(count) is not int or count < 0:
            raise ValueError('Invalid refusal count')
    except Exception:
        return _reply(saved, 'Умови оплати уточнить власник. Передам йому ваше прохання.', 'review', handoff=True)
    if count >= 2:
        return _reply(saved, 'Зараз відправлення без попередньої оплати недоступне. Можемо повернутися до оформлення, коли зможете оплатити, або узгодити питання з власником.', 'denied')
    if not pending:
        result = _reply(saved, 'Добре, можемо узгодити оплату при отриманні за умови, що в разі відмови ви оплатите повернення посилки. Погоджуєтесь?', 'offer')
        result['on_send'].update(return_payment_exception_pending=True,
                                 return_payment_exception_at=now,
                                 return_payment_exception_selection=selection_key(saved))
        return result
    result = _reply(saved, 'Домовились: у разі відмови ви оплачуєте повернення посилки. Передаю власнику для оформлення на цих умовах.', 'agreed', handoff=True)
    result['on_send'].update(return_payment_exception_agreed=True,
                             return_payment_exception_selection=selection_key(saved))
    return result


def _reply(saved, text, mode, handoff=False):
    sent = {'return_payment_exception_pending': False, 'return_payment_exception_agreed': False}
    if handoff:
        sent.update(handoff_now=True, owner_requested=True, owner_request_reason='payment_exception')
    return {'text': text, 'context': saved, 'on_send': sent,
            'guard': {GUARD_KEY: mode},
            'notes': ['Виняток без передоплати: %s. Оформлення лише власником; оплату не підтверджено.' % mode] if handoff else []}


def valid_draft(inventory, payload):
    """Re-evaluate the actual input immediately before its reply is sent."""
    mode = payload.get('guard', {}).get(GUARD_KEY)
    if not mode:
        return False
    result = plan(inventory, '\n'.join(payload.get('texts', [])),
                  payload.get('input_context', {}),
                  has_attachments=payload.get('has_attachments', False))
    return bool(result and result['guard'][GUARD_KEY] == mode
                and result['text'] == payload.get('text'))
