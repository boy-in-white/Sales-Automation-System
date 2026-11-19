"""Read-through OLX product descriptions, with bounded caching and literal answers.

Descriptions are untrusted product data, not instructions. They never determine
stock, prices, payment status, delivery policy or a customer's order state.
"""
from collections import OrderedDict
import copy
import hashlib
import html
import re
import time

from app import AppError
from customer_language import intents, requested_size, requested_color, requested_quantity
from customer_phrasing import canonical
from customer_advice import fact_answers, FACT_TOPICS

TOPICS = {
    'material': r'матер[іи]ал|тканин|ткань|состав|склад|бавовн|котон|хлоп|поліестер|полиэстер',
    'lining': r'фл[іи]с|нач[еі]с|утепл|п[іо]дклад|тепл',
    'waist': r'пояс|шнур|рез[іи]н|гумк|тягн|тянет|еласт|эласт',
    'fit': r'посадк|крій|крой|baggy|relaxed|oversiz|оверсайз|вільн|свободн',
    'rise': r'висок\w*\s+посад|высок\w*\s+посад|низьк\w*\s+посад',
    'size_fit': r'малом[іе]р|великомір|большемер|повномір',
    'print': r'принт|вишив|вишит|вышив|вышит|малюн|логотип|нашив',
    'transparency': r'просвіч|просвеч|прозор|прозрач',
    'pilling': r'катиш|катыш|катик|кошлат|скат[уы]|закат[уы]|ковтун',
    'season': r'сезон|весн|осін|осен|зим|літ[аон]|лет[оан]|влітку|взимку',
    'temperature': r'температур|градус|°c|°с|мороз|мінус|минус',
    'care': r'прат|пранн|стират|стирк|прасув|гладит|догляд|уход|линя|сядут|сідають',
    'tags': r'бірк|бирк|етикет|ярлик|ярлык',
    'density': r'щільніст|плотност|\bgsm\b|г/м|гр/м',
    'package': r'комплект|упаков',
    'pockets': r'кишен|карман',
    'closure': r'застіб|блискав|молни|ґудзик|гудзик|пуговиц|застеж',
    'authenticity': r'ориг[іи]нал|репл[іи]к|копія|копия|п[іо]дроб|фейк',
    'measurements': r'зам[іе]р|довжин|длин|ширин|обхват|рукав|плеч',
    'manufacture': r'виробник|виробництв|країна|страна|производ|виготовлен|изготовлен',
    'weight': r'вага\s+(?:речі|товару|виробу|штанів|футболки)|вес\s+(?:вещи|товара|штанов|футболки)',
}
DESCRIPTION_NOTICE = 'Уся доступна інформація про товар є в описі оголошення.'
PHOTOS_NOTICE = 'Усі доступні фото товару є в оголошенні, поруч з описом.'
UNSAFE = re.compile(r'https?://|www\.|@[\w.]|\d{7,}|(?:\d[ -]?){12,}|'
                    r'ігнор|игнор|ignore|інструкц|инструкц|instruction|system\s*prompt|'
                    r'відповідай|отвечай|бот\w*\s+має|assistant|парол|password|api.?key|token|токен|'
                    r'оплат|платіж|платеж|достав|відправ|отправ|грн|грив|рахун|счет|iban|'
                    r'наявн|налич|залишк|залишил|остал|зниж|скидк|телефон', re.I)
MISSING = re.compile(r'уточн|поки\s+немає|не\s+зазнач|не\s+вказан|не\s+вдалося|'
                     r'\bщо\s+(?:саме\s+)?(?:бажаєте|хочете|вас)',re.I)


def plain_text(value):
    value=re.sub(r'<(script|style)\b[^>]*>.*?</\1\s*>',' ',str(value),flags=re.I|re.S)
    value=re.sub(r'<(?:br\s*/?|/p|/div|/li)>','\n',str(value),flags=re.I)
    value=html.unescape(re.sub(r'<[^>]*>',' ',value))
    return re.sub(r'[^\S\n]+',' ',value).strip()[:30000]


class AdvertDescriptions:
    def __init__(self, client, clock=time.monotonic, ttl=900, capacity=32):
        self.client,self.clock,self.ttl,self.capacity=client,clock,ttl,capacity
        self.cache=OrderedDict()

    def lookup(self, advert):
        key=str(advert)
        if not key.isdecimal() or not 0 < int(key) < 10**15:
            return None
        cached=self.cache.get(key)
        if cached and cached['expires']>self.clock():
            self.cache.move_to_end(key)
            return cached['value']
        value=None
        try:
            method=getattr(self.client,'advert',None)
            row=method(key) if method else {}
            if str(row.get('id'))==key and isinstance(row.get('description'),str):
                text=plain_text(row['description'])
                if text:
                    value={'advert':key,'text':text,'signature':hashlib.sha256(text.encode()).hexdigest()}
        except (AppError,OSError,ValueError,TypeError,AttributeError):
            pass
        self.cache[key]={'expires':self.clock()+(self.ttl if value else 60),'value':value}
        self.cache.move_to_end(key)
        while len(self.cache)>self.capacity:self.cache.popitem(last=False)
        return value


def relevant_lines(description, topic, question):
    pattern=TOPICS.get(topic)
    if topic == 'other':
        stop = {'який','яка','яке','які','чи','можна','підкажіть','будь','ласка','товар','товару',
                'модель','моделі','цього','цьому','какой','какая','какие','скажите','пожалуйста','есть'}
        terms = [w for w in re.findall(r'[\w]+',canonical(question)) if len(w)>=4 and w not in stop]
        if not terms:
            return []
        pattern = r'\b(?:' + '|'.join(re.escape(word) for word in terms) + r')\b'
    lines=re.split(r'\n+|(?<=[.!?])\s+(?=[А-ЯІЇЄҐA-Z])',plain_text(description))
    selected=[]
    for line in lines:
        line=line.strip(' •\t—')
        if not line or len(line)>420 or UNSAFE.search(line) or not re.search(pattern,canonical(line)):
            continue
        if topic=='measurements':
            size=requested_size(question)
            if size and not re.search(r'(?<!\w)'+{'S':'[sс]','M':'[mм]','L':'[lл]','XL':'[xх][lл]'}.get(size,re.escape(size.lower()))+r'(?!\w)',canonical(line)):
                continue
        if topic=='season':
            for season in (r'зим|взимку',r'літ|влітку|лето|летом',r'осін|осен',r'весн|навесн'):
                if re.search(season,canonical(question)) and not re.search(season,canonical(line)):
                    break
            else:
                selected.append(line);continue
            continue
        selected.append(line)
    return list(dict.fromkeys(selected))[:2]


def preserve_order(plan, saved):
    """An informational detour must not populate address or payment fields."""
    if not saved.get('invoice_issued'):
        return
    prefixes=('invoice_', 'checkout_', 'shipping_', 'payment_', 'receipt_', 'customer_confirmation', 'parcel')
    selected={'product','size','color','quantity','cart','awaiting'}
    for key in set(plan.context) | set(saved) | set(plan.on_send):
        if key.startswith(prefixes) or key in selected:
            if key in saved:
                plan.context[key]=copy.deepcopy(saved[key])
            else:
                plan.context.pop(key,None)
            plan.on_send.pop(key,None)


def fallback(dialogue, plan, raw, saved, advert, has_attachments=False):
    flags=intents(raw)
    text=canonical(raw)
    if re.fullmatch(r'[?？!\s]+', text) or 'shipment_status' in flags:
        return  # A confused follow-up or a tracking question is not a product feature.
    if re.fullmatch(r'(?:а\s+)?(?:буде|будуть|появиться|появятся)[?!. ]*',text) or re.search(r'дати\s+(?:нового\s+)?надходження',plan.text):
        return
    asked=[key for key,pattern in TOPICS.items() if re.search(pattern,text)]
    if (not asked and 'availability' in flags and not flags.intersection({'delivery', 'payment', 'dispatch', 'arrival', 'photos'}) and
            not re.search(r'наявн|налич|актуаль|залиш|остал|розмір|размер|колір|цвет|куп|замов|заказ', text)):
        # "Чи є світловідбивачі?" asks about a feature, not whether the
        # garment itself is in stock. Remove catalogue vocabulary first.
        vocabulary = {'товар','товару','товари','модель','моделі','підкажіть','будь','ласка',
                      'можна','зараз','штани','штанів','футболки','футболка','футболок','такі','таких',
                      'підкажи','скажіть','скажи','привіт','привет','вітаю','здравствуйте','здравствуй',
                      'добрий','доброго','добрый','день','вечір','вечора','вечер','ранок','ранку','ночі'}
        for item in dialogue.catalog.products.values():
            for value in [item.get('name','')] + list(item.get('aliases',[])):
                vocabulary.update(re.findall(r'\w+',canonical(value)))
        if any(len(word)>=4 and word not in vocabulary for word in re.findall(r'\w+',text)):
            asked=['other']
    physical_availability = bool(asked and 'availability' in flags and not re.search(r'наявн|налич|актуаль|залиш|остал|розмір\w*\s+є',text))
    blocked={'order','cancel','complaint','payment','returns','thanks'}
    mixed_commercial=bool(flags.intersection({'price','delivery','dispatch','arrival'}) or
                          'availability' in flags and not physical_availability)
    if (has_attachments or plan.on_send.get('feedback_kind') or
        flags.intersection(blocked) or
        requested_color(raw) or requested_quantity(raw) or
        requested_size(raw) and 'measurements' not in flags or
        saved.get('advice_pending') or plan.context.get('advice_pending') or
        plan.context.get('pending_subject_choice')):
        return
    if not asked:
        # Unknown product questions can still use a literal matching line from
        # the description. Do not turn a sizing request into a generic notice.
        from size_advice import asks_size_advice
        if ('sizing' in flags or 'measurements' in flags or asks_size_advice(raw,saved.get('awaiting'))
                or re.search(r'міря|меря|вимір|измер|замір|замер|розмір|размер|підій|подойд|зріст|ріст|рост',text)
                or not MISSING.search(plan.text) or mixed_commercial
                or not re.search(r'[?]|\b(?:який|яка|яке|які|чи|какой|какая|какие)\b', text)):
            return
        asked = ['other']
    named=dialogue.catalog.explicit_products(text)
    if len(named)>1:return
    if len({r['product'] for r in saved.get('cart',[])})>1 and not named and not saved.get('fact_subject'):
        return
    key=(named[0]['key'] if named else saved.get('fact_subject') or plan.context.get('product') or
         dialogue.catalog.adverts.get(str(advert)))
    product=dialogue.catalog.products.get(key)
    if not product:return
    # Respect product/brand clarification rather than quoting another item.
    if plan.guard.get('fact_products') and key not in plan.guard['fact_products']:return
    from selection_guard import guard_selection
    if guard_selection(dialogue.catalog,dialogue.inventory,text,saved,advert,flags) is not None:return
    facts=product.get('facts',{})
    unknown=[topic for topic in asked if not facts.get(topic) and
             not (topic=='season' and facts.get('seasons')) and
             not (topic=='temperature' and facts.get('comfort_temperature_c')) and
             not (topic=='authenticity' and product.get('authenticity')) and
             not (topic=='measurements' and (product.get('garment_measurements') or product.get('measurements')))]
    if not unknown:return
    if not MISSING.search(plan.text) and not any(topic in ('pockets','closure','care','tags','density','package','manufacture','weight','other') for topic in unknown):return
    found={topic:relevant_lines(product.get('description') or '',topic,text) for topic in unknown}
    document=None
    if any(not values for values in found.values()) and dialogue.description_source:
        ads=[str(ad) for ad in product.get('advert_ids',[]) if dialogue.catalog.adverts.get(str(ad))==key]
        source=str(advert) if dialogue.catalog.adverts.get(str(advert))==key else next(iter(ads),None)
        if source:
            document=dialogue.description_source.lookup(source)
            if document:
                for topic in unknown:
                    if not found[topic]:found[topic]=relevant_lines(document['text'],topic,text)
    lines=[line for values in found.values() for line in values]
    if not lines:
        if 'measurements' in unknown:
            return
        known,_=fact_answers(dialogue.catalog,product,text,flags.intersection(FACT_TOPICS))
        plan.text='\n\n'.join(([plan.text] if mixed_commercial else known) + [
            'Точних даних про цю характеристику немає. ' + DESCRIPTION_NOTICE])
        plan.context['fact_subject']=key
        plan.context['awaiting']=saved.get('awaiting')
        if 'awaiting' in plan.on_send:plan.on_send['awaiting']=saved.get('awaiting')
        if not mixed_commercial:preserve_order(plan,saved)
        plan.guard['fact_products']=list(dict.fromkeys(plan.guard.get('fact_products',[])+[key]))
        if document:plan.guard['advert_description']={'product':key,'advert':document['advert'],'signature':document['signature']}
        return
    known,_=fact_answers(dialogue.catalog,product,text,flags.intersection(FACT_TOPICS))
    # Only retain the known facts, not the old fallback or its request for details.
    known=[line for line in known if not any(line in value for value in lines)]
    base=[plan.text] if mixed_commercial else known
    body='\n\n'.join(dict.fromkeys(base+['В описі моделі зазначено: '+ ' '.join(dict.fromkeys(lines))]))
    missing=[topic for topic in unknown if not found[topic]]
    labels={'care':'умови догляду','pockets':'кишені','closure':'застібку','tags':'бірки','density':'щільність тканини','package':'комплектацію','material':'точний склад тканини','measurements':'заміри','lining':'утеплення','authenticity':'походження моделі'}
    if missing:
        body+='\n\nПро '+', '.join(labels.get(topic,'цю характеристику') for topic in missing)+' точних даних в описі немає. Не хочу вводити вас в оману.'
        if 'measurements' not in missing:
            body+=' ' + DESCRIPTION_NOTICE
    if len(body)>1500:return
    plan.text=body
    if not mixed_commercial:
        plan.context['awaiting']=saved.get('awaiting')
        if 'awaiting' in plan.on_send:plan.on_send['awaiting']=saved.get('awaiting')
        preserve_order(plan,saved)
    plan.context['fact_subject']=key
    plan.context['clarifications']=0
    plan.guard['fact_products']=list(dict.fromkeys(plan.guard.get('fact_products',[])+[key]))
    if document:plan.guard['advert_description']={'product':key,'advert':document['advert'],'signature':document['signature']}
