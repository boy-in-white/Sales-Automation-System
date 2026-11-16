"""Customer-facing checkout for carrier delivery payments, with no bank deposit."""

import copy
import re

from carrier_payment import direct_delivery, tracking_url, enabled
from customer_phrasing import canonical, cod_deposit_question
from customer_language import reports_payment
from customer_contact import confirms
from order_summary import snapshot, summary, confirmation_key
from shop_facts import shipping_reply


RETIRED_NOTICE = ('Умови цього замовлення уточнить власник, щойно звільниться. '
                  'Поки нічого додатково не оплачуйте — перевіримо оформлення, щоб не було подвійної оплати.')


def faq(raw, saved, flags, active=True):
    # A prepared full-COD waybill must not silently become a deposit invoice.
    if not active and direct_delivery(saved):
        return RETIRED_NOTICE, True
    text = canonical(raw)
    parcel = saved.get('parcel', {})
    if re.search(r'(?:немає|нема|нет|неможливо|невозможно|відсутн|не\s+можу|не\s+могу|не\s+дає|не\s+працю|не\s+работ).{0,40}(?:оплат|кноп|сплат)|(?:оплат|кноп).{0,30}(?:недоступ|немає|нема|нет)|(?:лише|тільки|только).{0,30}(?:вс[еёю]|ус[ею]|з\s+товар|за\s+товар|разом|вместе)', text):
        return ('Спробуйте відкрити цю ТТН у застосунку Нової пошти під номером телефону отримувача. Якщо оплата доставки ще недоступна або пропонується лише разом із товаром, власник допоможе узгодити оформлення, щойно звільниться. Повторно нічого не оплачуйте.', True)
    if (reports_payment(text) or re.search(r'квитанц|квитанці|\bчек\b|оплатили|оплату\s+бач|грош[іи]\s+прийш', text)) and direct_delivery(saved):
        if saved.get('payment_verified'):
            return ('Оплату доставки підтверджено, дякую! Повторно оплачувати її не потрібно.', False)
        unclear = parcel.get('delivery_state') in ('waiting','unknown','revoked','unavailable')
        return ('Дякую! Оплату доставки звіримо за даними Нової пошти. Квитанцію надсилати не потрібно; повторно не оплачуйте.', unclear)
    if re.search(r'повер|верта|возвра|верн|відмовлю|відмова|откажусь|отказ|не\s+заберу', text) and re.search(r'достав|оплат|плат[іе]ж|кошти|гроші|деньг|\b100\b', text):
        return ('Доставку ви оплачуєте Новій пошті. Її повернення або перерахунок відбувається за правилами перевізника. Якщо потрібно скасувати замовлення — напишіть, допоможемо узгодити це до відправлення.', False)
    if cod_deposit_question(text, saved) or re.search(r'\b100\b|без\s+(?:передоплат|передплат|предоплат)|навіщо.{0,25}достав|зачем.{0,25}достав', text):
        return ('Переказувати нам 100 грн не потрібно. За післяплати доставку оплачуєте безпосередньо Новій пошті за ТТН, а товар — повністю при отриманні.', False)
    if re.search(r'реквіз|реквиз|рахун|сч[её]т|картк|\bкарту\b|iban|як.{0,20}(?:оплат|сплат)|как.{0,20}оплат', text):
        if parcel.get('ttn'):
            return ('За цим способом переказ на наш рахунок не потрібний. Відкрийте ТТН ' + parcel['ttn'] + ' у застосунку Нової пошти або на її офіційній сторінці:\n' + tracking_url(parcel['ttn']) + '\nОберіть оплату доставки. Якщо кнопка ще недоступна — напишіть, допоможу.', False)
        return ('Для післяплати банківські реквізити не потрібні: доставку оплачуєте Новій пошті за ТТН. Спочатку уточнимо дані й підготуємо накладну.', False)
    if direct_delivery(saved) and ('price' in flags or re.search(r'скільки.{0,30}(?:отриман|залиш|доплат)|сколько.{0,30}(?:получ|остат)|відніма|входить', text)):
        total = saved.get('invoice_total')
        return (('При отриманні за товар — %s грн. ' % total if total else '') + 'Доставка оплачується окремо Новій пошті й не віднімається від ціни товару. Комісія за післяплату — за тарифами NovaPay.', False)
    return None


def plan(checkout, raw, saved, flags, attachments=False):
    from checkout import shipping_details, missing_details_prompt, LABELS, method_choice
    from checkout_changes import quantity_change
    context, sent, guard = copy.deepcopy(saved), {}, {}
    text = canonical(raw)

    def reply(body, human=False):
        if human:
            sent.update(handoff_now=True)
        return {'text': body, 'context': context, 'on_send': sent, 'guard': guard, 'notes': []}

    if 'cancel' in flags:
        if saved.get('parcel', {}).get('ttn'):
            return reply('Допоможемо скасувати це відправлення й перевіримо стан ТТН у Новій пошті. Власник відпише, щойно звільниться.', True)
        from conversation_flow import fresh_selection
        context = fresh_selection(saved)
        return reply('Добре, це оформлення скасовано. Якщо захочете обрати іншу річ — допоможу.')
    if not enabled(checkout.catalog.shop):
        return reply(RETIRED_NOTICE, True)
    answer = faq(raw, saved, flags)
    if answer:
        return reply(*answer)
    choice = method_choice(raw)
    if choice in ('full','olx'):
        return reply('Можемо змінити спосіб оформлення. Власник допоможе узгодити його для цього ж замовлення, щоб не створити зайву накладну й не попросити оплату двічі.', True)
    if flags.intersection({'dispatch', 'arrival'}):
        if saved.get('payment_verified'):
            return reply('Дякую! ' + shipping_reply(checkout.catalog.shop, checkout.now() if checkout.now else None))
        return reply('Накладну підготуємо після підтвердження адреси. Перед відправленням потрібно підтвердити оплату доставки у Новій пошті.' if not saved.get('parcel', {}).get('ttn') else
                     'ТТН уже підготовлена. Очікуємо підтвердження оплати доставки від Нової пошти; після цього узгодимо відправлення.')
    if saved.get('customer_confirmation'):
        if re.search(r'змін|замін|прибери|додай|добав|передум|телефон|місто|город|село|відділен|отделен|поштомат|почтомат', text):
            return reply('Замовлення вже передано на підготовку ТТН. Власник допоможе узгодити зміни, щоб не залишити накладну зі старою адресою або сумою.', True)
        if confirms(text):
            return reply('Усе збережено. ' + ('ТТН є вище в чаті — за нею можна перевірити доступність оплати доставки.' if saved.get('parcel', {}).get('ttn') else 'Надішлю ТТН окремим повідомленням, щойно вона буде готова.'))
        return None
    change = quantity_change(checkout.catalog, checkout.inventory, text, context, attachments)
    if change is not None:
        body, human, changes_guard = change
        guard.update(changes_guard)
        if saved.get('parcel', {}).get('ttn'):
            return reply('ТТН уже підготовлена. Власник узгодить зміну кількості й суму післяплати.', True)
        return reply(body + '\n\n' + missing_details_prompt(context.get('shipping_details', {})) if any(not context.get('shipping_details', {}).get(k) for k in LABELS) else body, human)
    if flags.intersection({'sizing', 'measurements', 'material', 'availability', 'photos', 'authenticity', 'returns'}):
        return None
    if attachments and not raw.strip():
        return reply('Фото отримав. Для накладної потрібні дані текстом:\n' + missing_details_prompt(context.get('shipping_details', {})))
    details = shipping_details(raw, saved.get('shipping_details', {}), saved.get('shipping_awaiting'))
    context['shipping_details'] = details
    missing = [key for key in LABELS if not details.get(key)]
    if missing:
        context['shipping_awaiting'] = missing[0]
        sent['awaiting'] = 'shipping_details'
        return reply(missing_details_prompt(details))
    context.pop('shipping_awaiting', None)
    if checkout.address_book:
        from shipping_directory import details_key
        if context.get('shipping_directory_key') != details_key(details) or not details.get('warehouse_ref'):
            review = checkout.address_book.review(details, saved.get('shipping_validation'), text)
            if review.get('details'):
                details = context['shipping_details'] = review['details']
            if review['status'] != 'ready':
                context['shipping_validation'] = review
                context.pop('shipping_directory_key', None)
                context.pop('customer_confirmation', None)
                sent['awaiting'] = 'shipping_address'
                return reply(review['prompt'], review['status']=='unavailable')
            context['shipping_directory_key'] = details_key(details)
            context.pop('shipping_validation', None)
    order = snapshot(checkout.catalog, context)
    key = confirmation_key(order)
    if not attachments and saved.get('confirmation_sent') == key and details == saved.get('shipping_details', {}) and confirms(text):
        context['customer_confirmation'] = key
        sent.update(checkout_stage='submitted', awaiting=None, handoff_now=False)
        return reply('Дякую, замовлення №' + order['id'] + ' підтверджено! Підготую ТТН і надішлю її окремим повідомленням з підказкою щодо оплати доставки.')
    context.pop('customer_confirmation', None)
    sent.update(confirmation_sent=key, awaiting='order_confirmation')
    if saved.get('confirmation_sent') == key:
        return reply('Дані замовлення й адреса — вище. Якщо все правильно, напишіть «так»; якщо ні — лише те, що потрібно змінити.')
    return reply('Перевірте, будь ласка, замовлення й адресу:\n\n' + summary(order) + '\n\nУсе правильно? Напишіть «так» або що виправити.')
