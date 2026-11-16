"""Carrier-delivery payments are separate from payments for goods.

Shared verbatim with Ledgerly. Only authenticated, matching carrier evidence or
an explicit owner attestation can release an order for dispatch.
"""

from decimal import Decimal, InvalidOperation
import re
import time

POLICY = 'carrier_delivery'
FRESH_SECONDS = 180
ACTIVE_STATUSES = {'1', '4', '5', '6', '7', '8', '41', '101'}


def direct_delivery(value):
    return value.get('payment_policy') == POLICY or value.get('delivery_prepay') is True or value.get('checkout_delivery_prepay') is True


def enabled(shop):
    return shop.get('cod_payment') == POLICY


def tracking_url(ttn):
    if not re.fullmatch(r'[0-9]{14}', str(ttn)):
        raise ValueError('Invalid tracking number')
    return 'https://tracking.novaposhta.ua/#/uk/' + str(ttn)


def amount_cents(value):
    try:
        if isinstance(value, bool):
            return None
        amount = Decimal(str(value)) * 100
        if amount.is_finite() and 0 <= amount <= 100000000 and amount == amount.to_integral_value():
            return int(amount)
    except (ValueError, TypeError, InvalidOperation):
        pass
    return None


def evidence(document, row):
    """Fail closed: generic PaymentStatus, zero due or delivery status alone prove nothing.

    ExpressWaybillAmountToPay includes COD in observed live API responses. Thus
    NeedPayment cannot prove whether delivery alone is paid. Payed + zero owed
    is a conservative positive signal; ambiguous partial payments need review.
    """
    result = {'state': 'unknown', 'reason': 'payment_data_missing', 'cost_cents': None,
              'outstanding_cents': None, 'status_code': ''}
    if not isinstance(document, dict) or str(document.get('Number')) != str(row.get('ttn')):
        return dict(result, reason='tracking_identity_mismatch')
    ref = str(document.get('RefEW') or '')
    if ref and ref != '00000000-0000-0000-0000-000000000000' and ref.lower() != str(row.get('nova_ref', '')).lower():
        return dict(result, reason='tracking_reference_mismatch')
    phone = re.sub(r'\D', '', str(document.get('PhoneRecipient') or ''))
    expected = re.sub(r'\D', '', row.get('data', {}).get('recipient', {}).get('phone', ''))
    if phone and expected and phone != expected:
        return dict(result, reason='tracking_phone_mismatch')
    status = str(document.get('StatusCode', ''))
    result.update(status_code=status, cost_cents=amount_cents(document.get('DocumentCost')),
                  outstanding_cents=amount_cents(document.get('ExpressWaybillAmountToPay')))
    if status not in ACTIVE_STATUSES:
        return dict(result, state='unavailable', reason='shipment_not_active')
    if document.get('PayerType') != 'Recipient':
        return dict(result, reason='recipient_is_not_payer')
    if not result['cost_cents']:
        return result
    if document.get('ExpressWaybillPaymentStatus') == 'Payed' and result['outstanding_cents'] == 0:
        return dict(result, state='paid', reason='nova_confirmed')
    if document.get('ExpressWaybillPaymentStatus') == 'NeedPayment':
        return dict(result, state='waiting', reason='carrier_payment_not_confirmed')
    return result


def ready(row, now=None):
    if not row or row.get('nova_status') != 'created' or row.get('ledgerly_status') == 'cancelled':
        return False
    if not direct_delivery(row.get('data', {})):
        return bool(row.get('paid'))
    if row.get('delivery_state') == 'manual':
        return bool(row.get('paid') and row.get('paid_by') and row.get('delivery_checked'))
    current = time.time() if now is None else now
    return (bool(row.get('paid')) and row.get('delivery_state') in ('paid', 'manual') and
            current - FRESH_SECONDS <= row.get('delivery_checked', 0) <= current + 5)


def state_text(row):
    state = row.get('delivery_state', '')
    if row.get('ledgerly_status') == 'shipped' and state in ('paid','manual'):
        return '✅ Доставку було підтверджено перед відправленням'
    if state == 'paid' and 'delivery_checked' in row and not ready(row):
        return '⏳ Потрібно оновити підтвердження оплати доставки перед відправленням'
    if state == 'paid':
        return '✅ Оплату доставки підтвердила Нова пошта'
    if state == 'manual':
        return '✅ Доставку перевірено власником вручну'
    if state == 'revoked':
        return '⚠️ Оплата доставки більше не підтверджується — перевір ТТН'
    if state == 'unavailable':
        return '⚠️ НП не підтверджує активність цієї ТТН'
    if state == 'unknown':
        return '⏳ Автоматичного підтвердження доставки немає; можна перевірити вручну'
    return '⏳ Очікуємо підтвердження оплати доставки від НП'


def waybill_message(order_id, ttn, cost_cents=None):
    text = 'Ось ваша ТТН: ' + str(ttn) + '\nВаше замовлення №' + str(order_id)
    text += '\n\nДоставку можна оплатити в застосунку Нової пошти або на її офіційній сторінці:\n' + tracking_url(ttn)
    text += '\n\nОберіть оплату доставки. Якщо сервіс пропонує лише оплату разом із товаром або кнопка оплати ще недоступна — напишіть мені, допоможу.'
    text += '\nОплату перевіримо за даними Нової пошти — квитанцію надсилати не потрібно.'
    return text
