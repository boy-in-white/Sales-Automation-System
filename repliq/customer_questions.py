"""Contextual buyer questions found in the private OLX corpus.

Patterns come from incoming messages. Answers use only current shop facts and
the current order; historical seller promises are deliberately not a source.
"""

import copy
from decimal import Decimal, InvalidOperation
import re

from customer_phrasing import canonical, cod_mentioned, cod_deposit_question
from order_summary import snapshot
from shop_facts import money, shipping_reply, shop_day, FULL_PREPAYMENT_REFUND, COD_DEPOSIT_REFUND, COD_DEPOSIT_PURPOSE, COD_WITHOUT_PREPAYMENT
from selection_guard import selected_product
from olx_delivery import delivery_plan
from carrier_payment import direct_delivery, enabled as carrier_enabled


def question_plan(catalog, merchant, raw, saved, advert, flags, now=None, has_attachments=False):
    text = canonical(raw)
    context = copy.deepcopy(saved)
    named = catalog.explicit_products(text)
    product = named[0] if len(named) == 1 else selected_product(catalog, saved, advert)
    facts = product.get('facts', {}) if product else {}
    delivery = delivery_plan(catalog, raw, saved, now)
    if delivery is not None:
        return delivery

    def reply(body, human=False, updates=None, calendar=False):
        guard = {'shipping_day': str(shop_day(catalog.shop, now))} if calendar else {}
        if product:
            guard['fact_products'] = [product['key']]
        sent = dict(updates or {})
        if human:
            sent['handoff_now'] = True
        return {'text': body, 'context': context, 'on_send': sent, 'guard': guard, 'notes': []}

    if re.fullmatch(r'(?:а\s+)?(?:буде|будуть|появиться|появятся)[?!. ]*',text):
        previous=saved.get('conversation_memory',{}).get('turns',[])
        from customer_language import intents
        if saved.get('pending_alternatives') or previous and 'availability' in intents(previous[-1].get('customer','')):
            return reply('Підтвердженої дати надходження поки немає. Можу перевірити інший колір або розмір.')

    if re.search(r'(?:самому\s+верху|по\s+верхньому\s+краю).{0,25}м[іе]р|м[іе]р.{0,25}(?:самому\s+верху|по\s+верхньому\s+краю)',text):
        method = ('Пояс вказано як ширину штанів, розкладених на поверхні' +
                  (' без розтягування' if product.get('waist_measurement_state') == 'relaxed' else '') + '. '
                  if product and product.get('waist_measurement_kind') == 'flat_width' else '')
        return reply(method + 'Точну точку вимірювання у верхній частині виробу в описі не уточнено, тому не хочу ввести вас в оману.')

    if (not saved.get('invoice_issued') and 'delivery' in flags and
            not flags.intersection({'dispatch','arrival','cancel','returns'}) and
            ('?' in text or re.search(r'\bчи\b|можна|можно|відправляєте|отправляете',text)) and
            re.search(r'\b(?:олх|олікс|olx)\b', text) and not cod_mentioned(text) and
            not re.search(r'передоплат|предоплат|повн\w*\s+оплат|100\s*грн',text)):
        return reply('Так, відправляємо OLX Доставкою. Оформлення й оплата — у самому OLX.')

    if (not saved.get('invoice_issued') and not cod_deposit_question(text,saved) and
            re.search(r'(?:яка|какая|скільки|сколько|який\s+розмір).{0,20}(?:передоплат|передплат|предоплат)',text) and
            not re.search(r'повн\w*|полн\w*|олх|olx',text)):
        return reply('Для післяплати передоплата — 100 грн. Вона входить у вартість товару: решту сплачуєте при отриманні. Доставка оплачується окремо.')

    if (re.search(r'(?:розрахув|рассчит|оплат|сплат|заплат).{0,30}(?:отриман|получен|відділен|віділен|отделен|пошті|почте)|'
                  r'(?:отриман|получен|відділен|віділен|отделен|пошті|почте).{0,30}(?:розрахув|рассчит|оплат|сплат|заплат)', text) and
            not saved.get('invoice_issued')):
        explanation = ('Так, можна післяплатою. Передоплата — 100 грн, решту за товар сплачуєте при отриманні. Доставка оплачується окремо.'
                       if not carrier_enabled(catalog.shop) else
                       'Так, товар можна оплатити при отриманні. Перед відправленням потрібно оплатити доставку Новій пошті за ТТН.')
        items = saved.get('cart') or [saved]
        complete = all(all(row.get(key) for key in ('product','size','color')) for row in items)
        return reply(explanation + (' Оформлюємо так?' if complete else ''),
                     updates={'pending_checkout_method':'cod', 'checkout_offered':True} if complete else None)

    if not has_attachments and re.search(r'(?:дані|данные).{0,25}(?:сюди|сюда|вам|казати|писати|надсилати|отправ)|'
                                         r'(?:що|шо|что|які|какие).{0,15}(?:писати|дані|данные|надсилати)', text):
        if saved.get('checkout_method') == 'olx':
            return reply('Для OLX Доставки дані отримувача вказуєте під час оформлення в самому OLX. Тут повторювати їх не потрібно.')
        if saved.get('invoice_issued'):
            from checkout import missing_details_prompt
            details = saved.get('shipping_details', {})
            if all(details.get(key) for key in ('name', 'phone', 'place', 'branch')):
                return reply('Дані для відправки вже записані. Якщо потрібно щось змінити — напишіть, що саме.')
            return reply('Так, сюди в чат. ' + missing_details_prompt(details))
        return reply('Так, дані для відправки можна надіслати в цей чат. Спочатку погодимо товар і спосіб доставки.')

    if not has_attachments and re.fullmatch(r'(?:а\s+)?(?:до\s+скільки\s+зможете|орієнтов(?:но|ано)\s+(?:о\s+)?(?:в\s+)?скільки|чи\s+завтра)[?!. ]*', text):
        return reply(shipping_reply(catalog.shop, now) + ' Точну годину відправлення поки не обіцяю.', calendar=True)

    carrier_mode = direct_delivery(saved) or (carrier_enabled(catalog.shop) and not saved.get('invoice_issued') and
                   saved.get('checkout_amount') is None and saved.get('checkout_method') not in ('full','olx') and
                   not re.search(r'\b(?:olx|олх|олікс)\b', text) and
                   not re.search(r'повн\w*\s+(?:оплат|передоплат)|полн\w*\s+(?:оплат|предоплат)', text))
    if carrier_mode:
        from carrier_checkout import faq
        answer = faq(raw, saved, flags, active=carrier_enabled(catalog.shop))
        if answer:
            return reply(*answer)
        if direct_delivery(saved) and flags.intersection({'dispatch','arrival'}):
            return None
        if re.fullmatch(r'(?:давай(?:те)?\s+)?(?:по\s+)?(?:пп|фоп|передоплат\w*|предоплат\w*)[.!? ]*', text):
            return reply('Обираєте повну оплату товару за реквізитами чи післяплату з оплатою доставки Новій пошті за ТТН?')

    if re.fullmatch(r'(?:а?л[лоьо]+|ау(?:\s+брат)?|ну+|(?:ну\s+)?(?:що|шо|что)(?:\s+там)?|[?!.]+)[?!. ]*', text):
        if saved.get('owner_requested'):
            return reply('Власник відпише, щойно звільниться. Я поки можу допомогти із запитаннями про товар.')
        if saved.get('invoice_issued'):
            details = saved.get('shipping_details', {})
            labels = {'name': 'ПІБ', 'phone': 'номер телефону', 'place': 'населений пункт і область', 'branch': 'відділення або поштомат'}
            missing = [label for key, label in labels.items() if not details.get(key)]
            if missing:
                return reply('Я на зв’язку. Для оформлення ще потрібні: ' + ', '.join(missing) + '. Можна надіслати одним повідомленням.')
            if saved.get('parcel', {}).get('ledgerly_status') == 'shipped':
                return reply('Вашу посилку вже відправлено. Якщо потрібно щось уточнити — напишіть, будь ласка, що саме.')
            if direct_delivery(saved) and not saved.get('payment_verified'):
                return reply('ТТН підготовлена. Залишилося підтвердити оплату доставки в Новій пошті.' if saved.get('parcel', {}).get('ttn') else
                             'Дані замовлення збережені. Підготую ТТН і надішлю її окремим повідомленням.')
            return reply(shipping_reply(catalog.shop, now), calendar=True)
        if saved.get('awaiting') not in ('size', 'color'):
            return reply('Я на зв’язку. Підкажіть, будь ласка, що хочете уточнити?')

    if not flags.difference({'thanks', 'greeting'}) and re.search(r'гарн\w*\s+(?:дня|вечора|ночі|продаж|настрою)|хорош\w*\s+(?:дня|вечера|ночи)|до\s+(?:побачення|свидания)', text):
        return reply('Дякую, навзаєм! Звертайтеся 🙂')
    if not flags.difference({'thanks', 'greeting', 'order'}) and not re.search(r'\?', text) and re.search(r'подумаю|поміркую|потім\s+напишу|пізніше\s+напишу|потом\s+напишу|як\s+надумаю\s+напишу', text):
        return reply('Звісно, пишіть, коли буде зручно. Якщо знадобиться допомога з вибором — підкажу.')

    # Garment/model data are not the buyer's height, weight or desired size.
    if re.search(r'(?:на\s+(?:вас|вашому|людин|человек|модел)|у\s+модел).{0,35}(?:фото|розмір|размер|ріст|рост)|'
                 r'(?:розмір|размер|ріст|рост).{0,50}(?:на\s+фото|на\s+людин|на\s+модел|на\s+вас)|'
                 r'(?:який|какой).{0,15}(?:у\s+вас|ваш)\s+(?:ріст|рост)', text):
        if facts.get('photo_model'):
            return reply(facts['photo_model'])
        return reply('Розмір і параметри людини на фото уточню у власника. Для вашого підбору можемо орієнтуватися на заміри саме цієї моделі.', True)
    unknown_facts = [
        (r'бірк|бирк|етикет|этикет|ярлик|ярлык', 'tags', 'наявність і вигляд бірок'),
        (r'\bgsm\b|\bгсм\b|щільніст|плотност|ваг\w*\s+(?:штан|футбол|вироб)|вес\w*\s+(?:штан|футбол)', 'density', 'щільність тканини й вагу виробу'),
        (r'прат|пранн|стират|стирк|прасув|гладит|линя|сіда\w*\s+після|сядут', 'care', 'правила догляду за цією моделлю'),
        (r'в\s+комплект|у\s+комплект|що\s+в\s+упаков|что\s+в\s+упаков', 'package', 'комплектацію цієї моделі'),
    ]
    for pattern, key, label in unknown_facts:
        if re.search(pattern, text) and not flags.intersection({'complaint', 'returns'}):
            if key == 'care' and flags.intersection({'pilling','print'}) and not re.search(r'як\s+прати|как\s+стирать|температур.{0,12}(?:пран|стир)|режим.{0,12}(?:пран|стир)', text):
                continue
            if facts.get(key) and isinstance(facts[key], str):
                return reply(facts[key])
            known = facts.get('material', '') + ' ' if 'material' in flags and facts.get('material') else ''
            return reply(known + 'Уточню для вас %s, щоб відповісти точно.' % label, True)
    full_payment = saved.get('checkout_method') == 'full' or ('olx' not in text and 'олх' not in text and bool(re.search(r'повн\w*\s+(?:передоплат|оплат)|полн\w*\s+(?:предоплат|оплат)', text)))
    deposit_question = cod_deposit_question(text, saved)
    if not deposit_question and full_payment and re.search(r'повер|верта|возвра|верн|відмовлю|відмова|откажусь|отказ|не\s+заберу', text):
        return reply(FULL_PREPAYMENT_REFUND)
    if not deposit_question and re.search(r'повер|верта|возвра|верн|відмовлю|відмова|откажусь|отказ|не\s+заберу', text) and (saved.get('checkout_method') == 'cod' or re.search(r'передоплат|передплат|предоплат', text)):
        if saved.get('checkout_method') == 'cod' or re.search(r'\b100\b', text):
            return reply(COD_DEPOSIT_REFUND)
        return reply(COD_DEPOSIT_REFUND + ' За повної передоплати: ' + FULL_PREPAYMENT_REFUND)
    if ('olx' in text or 'олх' in text) and (cod_mentioned(text) or re.search(r'100\s*(?:грн|грив)|(?:передоплат|передплат).{0,15}100|передоплат\w*\s+через', text)):
        if carrier_enabled(catalog.shop) and not saved.get('invoice_issued'):
            return reply('OLX Доставка оформлюється з повною оплатою товару в самому OLX. За звичайної післяплати Новою поштою доставку оплачуєте за ТТН, а товар — при отриманні. Який спосіб вам зручніший?')
        return reply('OLX Доставка у нас оформляється з повною оплатою товару в самому OLX. Передоплата 100 грн — окремий варіант післяплати Новою поштою, решту за товар сплачуєте при отриманні. Який спосіб вам зручніший?')
    if deposit_question == 'without':
        return reply(COD_WITHOUT_PREPAYMENT)
    if deposit_question == 'purpose':
        return reply(COD_DEPOSIT_PURPOSE)
    if re.fullmatch(r'(?:давай(?:те)?\s+)?(?:по\s+)?(?:пп|фоп|передоплат\w*|предоплат\w*)[.!? ]*', text):
        return reply('Уточніть, будь ласка: передоплата 100 грн і решта при отриманні чи повна оплата товару?')
    if saved.get('invoice_issued') and not has_attachments and not direct_delivery(saved):
        if re.search(r'\bбанк\w*|приват|монобанк|\bмоно\b|privat|monobank', text):
            bank = 'monobank' if merchant.get('_monobank') else merchant.get('bank_name')
            return reply('Банк отримувача — ' + bank + '.' if bank else 'Назву банку отримувача уточню у власника. Реквізити вашого замовлення залишаються ті самі.', not bool(bank))
        if (re.search(r'квитанц|квитанці|\bчек\b', text) and
                ('?' in raw or re.search(r'\bчи\b|\bяк\b|\bкак\b|потріб|нуж|надо|треба', text)) and
                not re.search(r'не\s+(?:квитанц|квитанці|чек)|випадков|случайно', text)):
            if saved.get('checkout_method') == 'cod' and saved.get('bank_payment') and merchant.get('_monobank') and saved['bank_payment'].get('scope') == merchant['_monobank'].get('scope'):
                return reply('Так, надішліть фото або скріншот квитанції в цей чат. Надходження також перевіримо через mono; повторно оплачувати не потрібно.')
            return reply('Так, надішліть, будь ласка, фото або скриншот квитанції в цей чат. Саме надходження коштів перевіримо окремо.')
        if re.fullmatch(r'(?:(?:ок|все|всьо|всьо гроші|я|вже|уже|кошти|гроші)\s*[, ]*\s*)*(?:скину[вл][а]?|закинув|закинула|перекинув|перекинула|переказав|переказала|перев[еі]в|перевела)[.! ]*', text):
            return reply('Дякую, що повідомили! Перед відправкою звіримо надходження коштів.', updates={'payment_reported': True})
        if re.fullmatch(r'(?:прийшли|прийшло|прийшла|пришли|дійшли|дошли|отримали)[?!. ]*', text):
            return reply('Надходження коштів звіримо перед відправкою. Повторно оплачувати не потрібно.')
        if re.search(r'(?:передоплат|передплат|предоплат).{0,25}(?:віднім|вычит|вход|раху)|'
                     r'(?:до\s+сплати|при\s+отриман|на\s+пошті|на\s+почте|решт|остат).{0,30}(?:грн|грив|плат|сум)|'
                     r'(?:скільки|сколько).{0,30}(?:доплат|залиш|остан|при\s+отриман|на\s+пошті)|'
                     r'дешев.{0,55}(?:скинул|скинув|передоплат)', text):
            order = snapshot(catalog, saved)
            try:
                total, advance = Decimal(str(order['total'])), Decimal(str(order['amount']))
                if not total.is_finite() or not advance.is_finite() or not 0 <= advance <= total:
                    return reply('Погоджену суму звіримо з вашим замовленням. Повторно нічого не оплачуйте.', True)
            except (InvalidOperation, TypeError, ValueError):
                return reply('Погоджену суму звіримо з вашим замовленням. Повторно нічого не оплачуйте.', True)
            if order['method'] == 'cod':
                return reply('Так, передоплата входить у ціну товару: %s грн − %s грн = %s грн за товар при отриманні. Доставка оплачується окремо.' % (money(total), money(advance), money(total - advance)))
            if order['method'] == 'full':
                return reply('За цим способом товар оплачується повністю за реквізитами. Повторної оплати товару при отриманні немає — лише доставка.')
        if re.search(r'(?:яке|який|как[ао]я|как[оа]й).{0,18}(?:ім.я|прізвищ|фамили|отримувач|получател)', text):
            recipient = merchant.get('recipient')
            return reply('Отримувач переказу: ' + recipient + '.' if recipient else 'Ім’я отримувача переказу уточню у власника.', not bool(recipient))
    if 'arrival' in flags:
        if saved.get('parcel', {}).get('status') == 'cancelled':
            return reply('Це оформлення скасоване. Якщо бажаєте, допоможу оформити нове замовлення.')
        dispatched = saved.get('parcel', {}).get('ledgerly_status') == 'shipped'
        intro = 'Посилку вже відправлено.' if dispatched else shipping_reply(catalog.shop, now)
        return reply(intro + ' Точний день прибуття залежить від перевізника. Доставку до конкретної години чи дня не можу гарантувати.', calendar=not dispatched)
    if re.search(r'(?:скільки|сколько|вартіст|кошту|стоит|ц[іе]н).{0,25}(?:достав|пересил|пересыл)|'
                 r'(?:достав|пересил|пересыл).{0,25}(?:скільки|сколько|вартіст|кошту|стоит|ц[іе]н)', text):
        return reply(catalog.shop.get('delivery_cost') or 'Вартість доставки визначає перевізник під час оформлення. Вона оплачується окремо від вартості товару.')
    if 'dispatch' in flags and re.search(r'о\s+котрій|во\s+сколько|орієнтов|ор[іие]єнтован|до\s+(?:скільки|обіду)|точн|утром|зранку|вранці', text):
        if saved.get('parcel', {}).get('ledgerly_status') == 'shipped' or saved.get('parcel', {}).get('status') == 'cancelled':
            return None
        return reply(shipping_reply(catalog.shop, now) + ' Точну годину відправлення поки не обіцяю.', calendar=True)
    if re.search(r'зустр[іе]т|встрет|самовив|самовыв|під.їхат|под.ехать|можу.{0,15}забрати|район[іе]\b', text):
        policy = catalog.shop.get('pickup')
        return reply(policy if isinstance(policy, str) and policy else 'Можливість зустрічі або самовивозу потрібно узгодити з власником. Передам йому ваш запит.', not bool(policy))
    if re.search(r'ви\s+зв[іи]дки|вы\s+откуда|якого\s+міста|звідки\s+відправ|откуда\s+отправ', text):
        city = catalog.shop.get('dispatch_city')
        return reply('Відправляємо з міста ' + city + '.' if isinstance(city, str) and city else 'Місто відправлення уточню у власника.', not bool(city))
    if re.search(r'(?:коли|когда).{0,30}(?:будуть|буде|будут|будет|поставк|надходж)|'
                 r'(?:поставк|надходж).{0,20}(?:при[їе]х|буд)|(?:напиш|повідом).{0,25}(?:появ|з.яв|буд)', text) and 'dispatch' not in flags:
        return reply('Підтвердженої дати нового надходження поки не маю. Можу перевірити інший розмір або колір, який підійде вам зараз.')
    if re.search(r'(?:не\s+можу|не\s+могу|немає|нема|нет|не\s+пропонує|не\s+дає).{0,40}(?:оформ|замов|заказ|вибрати\s+розмір)|'
                 r'оголошення\s+неактив|объявление\s+неактив', text):
        return reply('Підкажіть, що саме показує OLX під час оформлення — текст помилки або скриншот. Якщо не дає вибрати розмір, погодимо його тут у чаті.')
    return None
