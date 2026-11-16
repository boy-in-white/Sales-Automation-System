"""Customer-requested human help and contextual recovery for uncertain messages."""

import re

from customer_phrasing import canonical
from shop_facts import measurements_reply


def requests_owner(raw):
    text = canonical(raw)
    # A quoted instruction or a refusal is not a request to contact someone.
    text = re.sub(r'[«"“].*?[»"”]', '', text)
    target = r'(?:власник\w*|хазяїн\w*|хозя[иї]н\w*|оператор\w*|менеджер\w*|продав[ец]\w*|людин\w*|человек\w*|адмін\w*|автор\w*|owner|human|manager|seller|operator)'
    clauses = [part.strip() for part in re.split(r'[,;!?]+|\s+(?:але|но|а)\s+',text) if part.strip()]
    if len(clauses)>1:
        if re.fullmatch(r'(?:(?:ні|нет|добре|ок|окей)\s+)?(?:поки\s+)?не\s+(?:треба|потрібно|надо)|передумав|передумала',clauses[-1]):
            return False
        for clause in reversed(clauses):
            if re.search(target+r'|не\s+хочу\s+бот',clause):
                return requests_owner(clause)
    if re.search(r'контакт|телефон|номер\s+(?:власник|продав)',text) and not re.search(r'поклич|позови|підключ|з.єднай|соедин',text):
        return False
    if re.search(r'(?:не\s+(?:хочу|треба|потріб|нуж|надо|поклич|клич|зови|підключ)|без\s+).{0,35}'+target, text):
        return False
    if re.search(target+r'.{0,20}(?:не\s+(?:треба|потріб|нуж|надо|клич)|не\s+хочу)', text):
        return False
    if re.search(r'(?:що\s+означає|як\s+працює|что\s+значит|как\s+работает|чи\s+може\s+бот).{0,50}'+target, text):
        return False
    if re.fullmatch(r'(?:будь\s+ласка\s+|пожалуйста\s+)?'+target+r'(?:\s+(?:будь\s+ласка|пожалуйста|please))?[!?., ]*', text):
        return True
    return bool(re.search(
        r'(?:поклич|покличте|позови|позовите|підключ|подключ|з.єднай|соедин|передай|передайте|запроси|поговорити|поговорить|поспілкуватися|поговорю|зв.язат|связат|напише|ответит|відповість).{0,45}'+target+'|'+
        r'(?:хочу|можна|можно|дайте|дай|потрібен|потрібна|нужен|нужна|хотів|хотел|зв.яжіть).{0,35}'+target+'|'+
        r'(?:нехай|хай|пусть|щоб).{0,25}'+target+r'.{0,25}(?:напиш|відпиш|відпов|ответ|допомож)|'+
        target+r'.{0,15}(?:поклич|позови|підключи)|'+
        r'(?:can\s+i\s+(?:speak|talk)|speak|talk|connect|contact|call|want|need).{0,30}'+target+'|'+
        r'(?:не\s+хочу\s+бот(?:а)?[!.,? ]*$|хочу\s+не\s+з\s+ботом[!.,? ]*$|не\s+з\s+ботом\s+хочу[!.,? ]*$)', text))


def abusive_only(raw):
    """Do not let insults reach the address parser or erase an active order."""
    from customer_language import intents, requested_size, requested_color
    text = canonical(raw)
    abuse = bool(re.search(r'\b(?:хуй|нахуй|похуй|пізд\w*|пизд\w*|єбав|ебав|ебал|їбав|бля(?:ть|дь)?|сука|лох|дебіл|дебил|ідіот|идиот|тупий|тупой)\b|в\s+рот\s+(?:[єїе]бав|[єїе]бал)', text))
    bulk_details = bool(re.search(r'разом|вместе|ус[еі]\s+дан|вс[еіёьо]+\s+(?:разом|сразу)|одним\s+повідом', text))
    meaningful = intents(text) - {'greeting','thanks','complaint'}
    return abuse and not bulk_details and not meaningful and not requested_size(text) and not requested_color(text)


def next_step(catalog, context, advert=None):
    """One relevant follow-up that preserves the current goods and delivery fields."""
    from checkout import LABELS
    product = catalog.products.get(context.get('product') or catalog.adverts.get(str(advert)),{})
    awaiting = context.get('awaiting') or ''
    if awaiting=='attachment_purpose':
        return 'Підкажіть, будь ласка, це квитанція про оплату чи інше фото? Якщо квитанція — достатньо написати «так, квитанція».'
    if context.get('invoice_issued'):
        details = context.get('shipping_details',{})
        missing = [key for key in LABELS if not details.get(key)]
        if missing:
            first = context.get('shipping_awaiting') if context.get('shipping_awaiting') in missing else missing[0]
            prompts = {'name':'Напишіть, будь ласка, прізвище, ім’я та по батькові отримувача.',
                       'phone':'Потрібен номер телефону отримувача. Можна написати у форматі 0671234567.',
                       'place':'Підкажіть населений пункт і область, куди відправляти. Для села додайте район.',
                       'branch':'Яке відділення або поштомат Нової пошти обираєте? Достатньо типу й номера.'}
            return prompts[first]
        if not context.get('customer_confirmation'):
            if context.get('confirmation_attempts',0)>=3:
                return 'Підтвердження поки немає. Коли звірите дані вище, напишіть «так» або що потрібно змінити — продовжимо оформлення.'
            return 'Перевірте товар і адресу в повідомленні вище. Якщо все правильно — напишіть «так». Для виправлення достатньо написати, що змінити.'
        if context.get('awaiting')=='payment_receipt' and not context.get('receipt_submitted'):
            return 'Замовлення й адресу вже записав. Після оплати надішліть, будь ласка, фото або скріншот квитанції в цей чат.'
        return 'Ваше замовлення вже записане. Що бажаєте уточнити щодо нього?'
    if context.get('checkout_offered'):
        return 'Обираєте OLX Доставку, післяплату з передоплатою 100 грн чи повну оплату? Можна написати «перший», «другий» або «третій».'
    if awaiting in ('parameters','height','weight') or context.get('size_advice_requested'):
        if context.get('height'):
            if context.get('weight') or context.get('weight_declined'):
                return 'Зріст і вагу вже врахував. Який розмір речей зазвичай носите? Можемо порівняти посадку за замірами цієї моделі.'
            return 'Підкажіть ще вагу, будь ласка. Якщо не знаєте — так і напишіть.'
        if context.get('weight'):
            return 'Підкажіть ще ваш зріст, будь ласка.'
        return 'Напишіть ваш зріст і вагу — допоможу підібрати розмір. Можна просто «180 80».'
    if awaiting in ('color','cart_color') or context.get('size') and not context.get('color'):
        return 'Який колір бажаєте? Можна написати його назву українською, російською або англійською.'
    if awaiting in ('quantity','cart_quantity'):
        return 'Скільки штук бажаєте? Напишіть кількість одним числом.'
    if context.get('clarifications',0)>=3:
        return 'Я на зв’язку. Коли будете готові продовжити, напишіть запитання про товар або бажані розмір і колір.'
    if product:
        if not context.get('size'):
            return 'Який розмір бажаєте? Якщо потрібна порада — напишіть ваш зріст і вагу.'
        return 'Що бажаєте уточнити щодо %s — наявність, заміри, матеріал чи оформлення?' % product['name']
    return 'Підкажіть, який товар вас цікавить і що хотіли б про нього дізнатися?'


def autonomous_reply(plan, catalog, saved, raw, advert=None):
    """Resolve a former automatic handoff without promising unavailable actions."""
    from customer_language import intents, reports_payment
    from customer_advice import fact_answers, FACT_TOPICS
    text = canonical(raw)
    flags = intents(text)
    context = plan.context
    product = catalog.products.get(context.get('product') or catalog.adverts.get(str(advert)),{})
    body = canonical(plan.text)
    if len(raw)>6000:
        return 'Отримав ваше повідомлення. Напишіть, будь ласка, найперше запитання окремо — відповім на нього й перейдемо до наступного.'
    if saved.get('checkout_delivery_prepay'):
        return 'Для цієї накладної спочатку потрібно звірити доступні способи оплати в Новій пошті. Поки нічого додатково не оплачуйте — це допоможе уникнути подвійного платежу.'
    if (saved.get('olx_notice_received') or saved.get('olx_delivery_reported')) and saved.get('checkout_method')=='olx':
        return 'Замовлення вже оформлене в OLX. Перш ніж змінювати доставку, потрібно звірити його стан у застосунку. Повторно оплачувати не потрібно.'
    if flags.intersection({'cancel','returns','complaint'}):
        if 'cancel' in flags and saved.get('invoice_issued'):
            context['cancellation_requested'] = True
            context.pop('customer_confirmation', None)
            return 'Запит на скасування записав. Замовлення вже оформлене, тому спочатку потрібно звірити його стан і оплату. Повторний переказ не потрібний.'
        return 'Допоможу розібратися. Напишіть, будь ласка, що саме сталося з товаром і чи вже отримали посилку. За можливості додайте фото.'
    if reports_payment(text) and not saved.get('invoice_issued'):
        return 'Дякую, що повідомили. Надішліть квитанцію та номер замовлення або ТТН, будь ласка — потрібно зіставити переказ із вашим замовленням. Повторно платити не потрібно.'
    if saved.get('parcel',{}).get('ttn') and re.search(r'змін|замін|додай|добав|прибери|убери|новий\s+телефон',text):
        return 'Зміну записав. Для цього замовлення вже підготовлено накладну, тому потрібно звірити її перед оновленням. Поточні дані відправлення поки діють. Повторно оплачувати не потрібно.'
    if 'photos' in flags:
        return 'Доступні фото є в оголошенні. Яку деталь хочете роздивитися або уточнити? Підкажу за наявними характеристиками.'
    if 'іншої марки' in body or 'іншої речі' in body:
        names = ', '.join(p['name'] for p in list(catalog.products.values())[:6])
        return 'Зараз у каталозі є %s. Про який із цих товарів розповісти?' % names
    if 'на фото' in text and re.search(r'ріст|зріст|рост|розмір|размер',text):
        return 'Параметри людини на фото не зазначені. Для підбору вам можемо порівняти заміри саме цієї моделі. Напишіть ваш зріст і вагу, будь ласка.'
    unknown = re.search(r'уточню для вас (.+?), щоб відповісти точно\.',body)
    if unknown:
        known = product.get('facts',{}).get('material','') if 'material' in flags else ''
        return (known+' ' if known else '')+'Точних даних про '+unknown[1]+' поки немає. Можу підказати склад і заміри цієї моделі.'
    if re.search(r'зустр[іе]т|самовив|самовыв|під.їхат|под.ехать',text):
        return 'Умови особистої зустрічі поки не погоджені. Можу оформити відправлення через OLX Доставку або Нову пошту.'
    if re.search(r'якого\s+міста|звідки|откуда',text):
        return 'Місто відправлення поки не зазначене. Після оформлення його буде видно в накладній.'
    if 'ім’я отримувача переказу' in body or 'назву банку' in body:
        return 'Додаткові дані отримувача переказу поки не зазначені. Якщо банк не дозволяє виконати платіж, надішліть текст помилки — допоможу розібратися.'
    if product and flags.intersection(FACT_TOPICS):
        answers, missing = fact_answers(catalog,product,text,flags)
        if missing:
            return ' '.join(answers + ['Точних даних про %s поки немає. Можу підказати склад, заміри та наявні варіанти цієї моделі.' % ', '.join(missing)])
    if product and flags.intersection({'sizing','measurements'}):
        measured, _ = measurements_reply(product)
        if measured:
            return 'За цими параметрами краще порівняти посадку зі зручною річчю. Ось точні заміри нашої моделі\n'+measured+'\n\nЯкий розмір за цими замірами вам ближчий?'
        return 'Точних замірів цієї моделі поки немає. Який розмір зазвичай носите і яку посадку любите — звичайну чи вільнішу?'
    if 'реквізит' in body or 'реквизит' in body:
        return 'Зараз потрібно звірити реквізити саме цього замовлення. Поки утримайтеся від переказу. Для товарів із цього оголошення також доступне оформлення OLX Доставки в застосунку.'
    if context.get('quantity_change'):
        return 'Зміну кількості записав. Спочатку звіримо вже сплачену суму, щоб правильно оновити замовлення. Повторно оплачувати не потрібно.'
    if flags.intersection({'price'}) or 'ціну' in body or 'вартість' in body:
        return 'Точну суму цього замовлення поки не можу підтвердити. До узгодження суми оплату робити не потрібно. Який саме товар і кількість бажаєте?'
    if 'sql' in ' '.join(plan.notes).lower() or 'наявність' in body and re.search(r'уточн|недоступ',body):
        return 'Зараз не вдалося отримати актуальну наявність. Напишіть бажані розмір і колір — перевірю ще раз перед оформленням.'
    if 'shipment_status' in flags:
        ttn = str(saved.get('parcel',{}).get('ttn',''))
        if re.fullmatch(r'\d{14}',ttn):
            return 'Ваша ТТН '+ttn+'. Актуальний рух посилки можна перевірити в застосунку перевізника. Сам факт створення накладної ще не підтверджує передачу посилки.'
        return 'Номера відправлення для цього замовлення поки немає. Після створення накладної надішлю його окремим повідомленням.'
    if flags.intersection({'arrival','dispatch'}):
        from shop_facts import shipping_reply
        return shipping_reply(catalog.shop)+' Точний термін у дорозі залежить від перевізника й населеного пункту.'
    if re.search(r'заміни|замініть|змін|додай|добав|прибери|убери',text):
        return 'Уточніть зміну одним рядком — товар, розмір, колір і кількість. Якщо це адреса, достатньо написати лише поле, яке змінюємо.'
    if plan.guard.get('stocks') or plan.guard.get('product'):
        return plan.text.replace('особисто','').replace('Передам запит власнику.','').strip()
    return next_step(catalog,dict(context,**{k:v for k,v in plan.on_send.items() if k=='awaiting'}),advert)
