"""Bounded Ukrainian/Russian customer input parsing; no external services."""

import re

from catalog import normalize
from customer_phrasing import canonical, cod_mentioned, customer_greeting


def parameters(text, context, weight_for_fit=True):
    """Interpret labelled units first; never extract measurements from a phone/address."""
    text = canonical(text)
    # Customers often separate the metre and centimetre parts with a space.
    text = re.sub(r'(?<!\d)([12])\s+(\d{2})(?!\d)', r'\1.\2', text)
    text = re.sub(r'\bвагою\b', 'вага', text)
    result, errors = {}, []
    number = r"(\d{1,3}(?:[.,]\d{1,2})?)"
    patterns = {
        "height": [r"(?:зріст|зростом|зросту|рост|ростом|ріст)\s*[:=—-]?\s*" + number,
                   number + r"\s*(?:см|cm|сантиметр\w*|метр\w*|м|m)(?!\w)"],
        "weight": [r"(?:вага|вагу|ваги|важу|вес|вешу|весом)\s*[:=—-]?\s*" + number,
                   number + r"\s*(?:кг|kg|кілограм\w*|килограмм\w*)(?!\w)"],
    }
    bridge = r"\s*[:=—-]?\s*(?:(?:у|в)\s+(?:мене|меня)\s*)?(?:становить\s*)?"
    patterns["height"][0] = r"(?:зріст|зростом|зросту|рост|ростом|ріст)" + bridge + number
    patterns["weight"][0] = r"(?:вага|вагу|ваги|важу|вес|вешу|весом)" + bridge + number
    garment_question = bool(re.search(r"довжин|длин|ширин|пояс|штанин|зам[іе]р|обхват|груд|талі|тали", text))
    for key, variants in patterns.items():
        matches = []
        for pattern in variants:
            if key == "height" and pattern == variants[-1] and garment_question:
                continue
            matches = [(m.start(), float(m.group(1).replace(",", "."))) for m in re.finditer(pattern, text)]
            if matches:
                break
        if matches:
            value = sorted(matches)[-1][1]
            result[key] = round(value * 100, 2) if key == "height" and value < 3 else value
    labelled_pair = re.search(r'(?:зріст|ріст|рост)(?:\s*(?:і|и|та|/)?\s*(?:вага|вес))?\s*[:=—-]?\s*' +
                              number + r'\s*[/;]\s*' + number, text)
    if labelled_pair:
        h, w = (float(labelled_pair.group(i).replace(',', '.')) for i in (1, 2))
        result.update(height=round(h * 100, 2) if h < 3 else h, weight=w)
    # A bare pair is accepted only when it comprises the entire message.
    pair = re.fullmatch(r"(?:(?:на|у мене|я|давай|давайте|добре|окей|ок|так|ось|тоді|ну)\s*[, :]?[ ]*){0,3}" +
                        number + r"\s*(?:[/;,\s]|\bі\b|\bи\b|\bта\b)\s*" + number + r"[.!]?", text)
    if pair and not result:
        h, w = (float(pair.group(i).replace(",", ".")) for i in (1, 2))
        if 3 <= h < 120 and 120 <= w <= 230:
            h, w = w, h
        result = {"height": round(h * 100, 2) if h < 3 else h, "weight": w}
    if not result and re.fullmatch(number + r"[.!]?", text) and context.get("awaiting") in ("height", "weight", "parameters"):
        value = float(text.rstrip(".!").replace(",", "."))
        if context.get("awaiting") == "weight":
            result["weight"] = value
        elif context.get("awaiting") == "height":
            result["height"] = round(value * 100, 2) if value < 3 else value
        elif value < 3 or 120 <= value <= 230:
            result["height"] = round(value * 100, 2) if value < 3 else value
        elif context.get("awaiting") == "weight" or context.get("height"):
            result["weight"] = value
        else:
            result['weight'] = value
    for key, low, high in (("height", 120, 230), ("weight", 35, 200)):
        if key in result and not low <= result[key] <= high:
            if key == "weight" and not weight_for_fit:
                if not 20 <= result[key] <= 300:
                    del result[key]
                    errors.append('Підкажіть, будь ласка, вагу ще раз — здається, у числі є описка.')
                continue
            del result[key]
            errors.append("Перевірте, будь ласка, зріст і вагу. Можна написати, наприклад, «175 68».")
    multi_person = re.search(r"(?:для двох|нас двоє|на двоих|для брата і|для меня и)", text)
    if not multi_person and len(re.findall(number, text)) >= 3:
        multi_person = re.search(r'(?:\bя\b|мене|меня).{0,80}(?:брат|сестр|мам|тат|друг|подруг)|'
                                 r'(?:брат|сестр|мам|тат|друг|подруг).{0,80}(?:\bя\b|мене|меня)', text)
    if multi_person:
        result.clear()
        errors = ["Давайте підберемо по черзі. Для кого зараз обираємо — які зріст і вага цієї людини?"]
    return result, errors


def requested_size(text):
    text = canonical(text)
    names = {
        "XS": ("xs", "хс", "ікс ес", "икс эс"),
        "S": ("s", "с", "еска", "эска", "ес", "эс"),
        "M": ("m", "м", "емка", "эмка", "ем", "эм"),
        "L": ("l", "л", "елка", "элька", "ель", "эль"),
        "XL": ("xl", "хл", "іксель", "икс эль"),
        "XXL": ("xxl", "ххл"),
    }
    found = [(m.start(), size) for size, aliases in names.items() for alias in aliases
             for m in re.finditer(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text)]
    found = [(position, size) for position, size in found
             if not re.search(r"(?:не|замість|вместо|крім|кроме)\s*$", text[max(0, position - 20):position])
             and not (size == "S" and re.match(r"с\s+(?:достав|оплат|предоплат|налож|олх|olx|утра)", text[position:]))
             and not (size == 'M' and text[position:position+1] in ('м','m') and re.search(r'\d(?:[.,]\d+)?\s*$', text[:position]))]
    return max(found)[1] if found else None


def requested_sizes(text):
    text = canonical(text)
    values = []
    # Inspect tokens independently while keeping the original negation context.
    for match in re.finditer(r"(?<!\w)(?:xxl|ххл|xl|хл|xs|хс|ікс ес|икс эс|іксель|икс эль|еска|эска|емка|эмка|елка|элька|[smlсмл])(?!\w)", text):
        if match[0] in ('м','m') and re.search(r'\d(?:[.,]\d+)?\s*$', text[:match.start()]):
            continue
        if re.search(r"(?:не|замість|вместо|крім|кроме)\s*$", text[max(0, match.start() - 20):match.start()]):
            continue
        if re.match(r"с\s+(?:достав|оплат|предоплат|налож|олх|olx|утра)", text[match.start():]):
            continue
        value = requested_size(match.group())
        if value and value not in values:
            values.append(value)
    return values


def requested_quantity(text):
    text = canonical(text)
    body, _ = parameters(text, {})
    if 'height' in body and 'weight' in body:
        return None  # "давай 180 і 80" answers the size question, not 180 units.
    # Keep counts attached to a product, allowing common intervening attributes.
    # Arbitrary words would turn phone numbers, prices and branch numbers into counts.
    attributes = r'(?:(?:біл\w*|бел\w*|чорн\w*|черн\w*|сір\w*|сер\w*|black|white|gr[ae]y|[sml]|xl)\s+){0,2}'
    goods = r'(?:шт\.?|штук\w*|пар[аиу]?|футбол\w*|штан\w*|suvene|gallery)(?!\w)'
    if re.search(r"\d+[.,]\d+\s*" + attributes + goods, text):
        return -1
    match = re.search(r"(?<![\d.,])(\d{1,3})\s*" + attributes + goods, text)
    if match:
        return -int(match[1]) if match.start() and text[match.start() - 1] in ("-", "−") else int(match[1])
    for pattern, amount in ((r"\b(?:дві|два|две|пару)\b", 2), (r"\bтри\b", 3), (r"\b(?:чотири|четыре)\b", 4)):
        if re.search(pattern + r"\s*" + attributes + goods, text):
            return amount
    words = {"одну": 1, "один": 1, "одна": 1, "дві": 2, "два": 2, "две": 2, "пару": 2, "три": 3, "чотири": 4, "четыре": 4}
    count = r"(\d{1,3}|одну|один|одна|дві|два|две|пару|три|чотири|четыре)"
    for pattern in (r"\b(?:хочу|беру|візьму|возьму|давай|давайте|додай|добав|зробіть|зроби)\s+(?:(?:замовити|заказать)\s+)?(?:ще\s+|еще\s+)?" + count + r"\b",
                    r"\b" + count + r"\s+(?:замовити|замовлю|заказать|закажу)\b"):
        match = re.search(pattern, text)
        if match and not re.search(r"\bне\s*$", text[max(0, match.start() - 10):match.start()]) and not re.match(r"\s*(?:грн|грив|uah|₴|варіант|вариант|спосіб|способ)", text[match.end():]):
            return int(match[1]) if match[1].isdecimal() else words[match[1]]
    return None


def color_mentions(text):
    text = canonical(text)
    matches = []
    for code, pattern in (("BLACK", r"чорн\w*|черн\w*|black"),
                          ("WHITE", r"біл\w*|бел\w*|white"),
                          ("GRAY", r"сір\w*|сер(?:ый|ые|ого|ая|ом|ую|ых|ий|і|ыє)|gr[ae]y"),
                          ("BLUE", r"син[іи]\w*|blue"), ("BEIGE", r"беж\w*|beige"),
                          ("GREEN", r"зелен\w*|green"), ("RED", r"червон\w*|красн\w*|red"),
                          ("BROWN", r"коричнев\w*|brown"), ("PINK", r"рожев\w*|розов\w*|pink"),
                          ("YELLOW", r"жовт\w*|желт\w*|yellow"), ("CREAM", r"молочн\w*|cream"),
                          ("PURPLE", r"ф[іи]олет\w*|ф[іи]ял[еє]т\w*|purple|violet"),
                          ("ORANGE", r"помаранч\w*|оранж\w*|orange"), ("BURGUNDY", r"бордов\w*|бургунд\w*|burgundy"),
                          ("TURQUOISE", r"б[іи]рюз\w*|turquoise"), ("KHAKI", r"хак[іи]|khaki"),
                          ("OLIVE", r"оливков\w*|olive"), ("LAVENDER", r"лаванд\w*|lavender"),
                          ("GOLD", r"золот\w*|gold"), ("SILVER", r"ср[іе]б\w*|серебр\w*|silver")):
        matches.extend((m.start(), code) for m in re.finditer(r"\b(?:" + pattern + r")\b", text)
                       if not re.search(r"(?:не|замість|вместо|крім|кроме)\s*$", text[max(0, m.start() - 20):m.start()]))
    return matches


def requested_colors(text):
    return list(dict.fromkeys(code for _, code in sorted(color_mentions(text))))


def requested_color(text):
    matches = color_mentions(text)
    return max(matches)[1] if matches else None


def intents(text):
    text = canonical(text)
    standalone_cancel = bool(re.fullmatch(r'(?:(?:добре|ок|окей|тоді|будь ласка|пожалуйста)\s*[, ]*\s*)*(?:скасуй|скасуйте|скасувати|скасовую|скасування|відміна|відмінити|відміни|відмініть|отмена|отмени|отмените|отменить|стоп)[.! ]*', text))
    patterns = {
        "greeting": r"добр(?:ий|ого|ої)\s+(?:день|дня|вечір|вечора|ранок|ранку|ночі)|добр[ыи]й\s+(?:день|вечер)|доброе\s+утро|прив[іе]т|вітаю|здравств|доброго времени|салют|\bhello\b|\bhi\b",
        "availability": r"наявн|налич|в наяв|в наявн|\bє\b|есть|доступн|залиш|остал|актуальн|(?:ще|еще|ещё)\s+прода[єе]те|не\s+продан",
        "sizing": r"розмір|размер|підій|подойд|п[іо]д[іо]бр|посадк|сид[іе]т|нал[іе]з|зріст|зрост|\bріст\b|\bрост\b|ваг[ауи]|\bвес\b|вешу|важу",
        "price": r"ц[іе]н|варт[іо]ст|кошту|стоит|ск[іо]льк\w*\s+(?:кош|стоит)|ск[іо]к\w*\s+стоит|поч[ео]м|пачом|price",
        "dispatch": r"(?:коли|когда|сьогодні|сьодні|сегодня|завтра|можете|можна|можете ли|скоро|о котрій|во сколько|скільки).{0,45}(?:відправ|отправ|надішл|вишл|вышл)|(?:відправ|отправ|надішл|вишл|вышл).{0,30}(?:сьогодні|сегодня|завтра|коли|когда|скоро)|\b(?:відправка|отправка)\b",
        "shipment_status": r"(?:вже|уже).{0,20}(?:відправ|отправ)|(?:відправ|отправ).{0,20}(?:вже|уже)|\b(?:відправили|отправили|відправлено|отправлено|ттн)\b|трек.{0,12}номер|номер.{0,12}(?:накладн|накладно|відправлен|отправлен)|де\s+(?:моя\s+)?посил|где\s+(?:моя\s+)?посыл",
        "arrival": r"коли\s+(?:при[їе]д|буде.{0,10}пош|отрима)|когда\s+(?:приед|получ)|скільки.{0,15}дн|сколько.{0,15}дн|через\s+(?:скільки|ск[іи]ки|сколько).{0,20}(?:при[їе]д|прийд|достав)|(?:до\s+(?:завтра|суботи|п.ятниц|понеділ|вівтор|серед|четвер|неділ)|завтра).{0,25}(?:при[їе]д|прийд|встиг|успе)|(?:при[їе]д|прийд|встиг|успе).{0,25}до\s+(?:завтра|суботи|п.ятниц)",
        "delivery": r"достав|відправ|отправ|надішл|вишл|вышл|пошто|почт|налож|накладен|коли\s+при[їе]д|когда\s+приед|самовив|самовыв",
        "payment": r"оплат|передплат|предоплат|післяплат|послеплат|платит|платити|реквіз|реквиз|рахун|сч[её]т|картк|\bкарту\b|квитанц|квитанці",
        "returns": r"повернен|повернут|возврат|вернуть|обм[іе]н",
        "description": r"матер[іи]ал|тканин|ткань|склад\s+ткан|состав|бавовн|хлопок|якіст|качеств",
        "measurements": r"зам[іе]р|довжин|длин|ширин|обхват|талі[яї]|тали[яи]|груд",
        "authenticity": r"ориг[іи]нал|репл[іи]к|п[іо]дроб|фейк|брендов",
        "discount": r"торг|зниж|скид(?:к|оч)|уступ|дешев|дешевш|(?:остання|крайня|последняя|остаточна|окончательная|край)\s+ц[іе]на|ц[іе]на\s+(?:остаточна|окончательная)|за\s+\d+\s*(?:грн\s*)?(?:віддасте|відасте|отдадите|заберу|беру)|скине(?:те|ш).{0,12}\d+",
        "material": r"матер[іи]ал|тканин|ткань|склад\s+(?:ткан|штан)|\bсклад(?:у)?[?! .]*$|состав|бавовн|хлопок|кот+он|полі[еє]стер|поли[еэ]стер|з\s+чого\s+(?:зробл|шит|пошит)|из\s+чего\s+(?:сшит|сделан)",
        "waist": r"пояс|резинк|резінк|гумк|тягн|розтяг|растяг|тян[еу]|эласт|еласт|шнур|зав.?язк",
        "lining": r"фл[іи]с|нач[іе][сс]?|утепл|тепл[іи]й?\b|теплы|з\s+п[іо]дклад|на\s+п[іо]дклад",
        "season": r"\b(?:сезон\w*|дем[іи]сезон\w*|зим\w*|взимку|весн\w*|навесн\w*|весенн\w*|осін\w*|осен\w*|наосінь|літо|літа|літку|літні\w*|влітку|лето|летом|летни\w*)\b",
        "pilling": r"катиш|катыш|катик|кошлат|скочуют|скатыва|скачуют",
        "quality": r"якіс|як[іо]ст|качеств|надійн|надежн",
        "fit": r"\bпосадк|\bкрій\b|\bкрой\b|\bbaggy\b|\bбаг[г]?і\b|\bбэгги\b|\brelaxed\b|\bоверсайз\b|\boversiz|\bширок\w*|\bвільн\w*|\bсвободн\w*|як\s+сид|как\s+сид",
        "rise": r"(?:висок|низьк|середн|высок|низк|средн)\w*.{0,12}посад|посад\w*.{0,12}(?:висок|низьк|высок|низк)",
        "size_fit": r"маломір|великомір|б[іо]льш[ео]м[іе]р|маломер|размер\s+в\s+размер|розмір\s+у\s+розмір|повномір",
        "temperature": r"температур|градус|\bмороз\w*|(?:при|на|у|в)\s*[+−-]\s*\d{1,2}\b|\bмінус\s*\d|\bминус\s*\d",
        "print": r"принт|вишив|вишит|вышив|вышит|нашив|логотип|малюн|рисунок|малюнок",
        "transparency": r"просвіч|просвеч|прозор|прозрач|св[іе]тит(?:ь)?ся",
        "photos": r"фото|фотк|в[іи]део|покаж",
        "order": r"замов|заказ|оформ|\bберу\b|заберу|хочу\s+(?:(?:ще|також)\s+)?(?:куп|штан|футбол|брюк|чорн|черн|сір|сер|біл|бел|suvene|gallery)|куплю|(?:можна|можно|готов).{0,12}(?:купити|купить)",
        "cancel": r"(?:^|\s)не\s+(?:треба|потрібно|нужно|хочу|надо)[.!]?\s*$|скасу\w*\s+замов|отмен\w*\s+заказ|неактуаль|(?:передумав|передумала|передумал|передумали)\s+(?:замовляти|купувати|заказывать|брати|брать)|не\s+(?:буду|хочу)\s+(?:замовляти|купувати|заказывать|брати|брать)",
        "thanks": r"дяку|спасиб|благодар|^\s*(?:красавчик|красавец|молодець|молодец|красава|ти\s+топ|ви\s+топ)[!.,🙂😊 ]*$",
        "complaint": r"скарг|жалоб|обман|повреж|пошкод|не\s+прийш|не\s+приш|де\s+мо[єя]|где\s+мо[йяе]|номер\s+(?:замов|заказ)|статус\s+(?:замов|заказ)|оператор|жив[ао]й\s+(?:людин|человек)",
    }
    found = {name for name, pattern in patterns.items() if re.search(pattern, text)}
    if re.search(r'(?:завтра|сьогодні|сегодня).{0,40}(?:при[їе]ха|отримат|получит|доставил)', text):
        found.add('arrival')
    if standalone_cancel:
        found.add('cancel')
    if customer_greeting(text):
        found.add('greeting')
    if cod_mentioned(text):
        found.update(("delivery", "payment"))
    if re.search(r"розмірн\w*\s+с[іе]тк|размерн\w*\s+сетк|мірн\w*\s+сітк", text):
        found.add("measurements")
    if re.search(r"(?:передоплат|передплат|предоплат).{0,45}(?:верта|повер|возвра|верн)|(?:верта|повер|возвра|верн).{0,45}(?:передоплат|передплат|предоплат)", text):
        found.add("returns")
    if found.intersection({"size_fit", "fit"}) and not re.search(r"який\s+(?:мені\s+)?розмір|какой\s+(?:мне\s+)?размер|зріст|\bріст\b|\bрост\b|\bваг[ауи]\b|\bвес\b", text):
        found.discard("sizing")
    if "season" in found and not re.search(r"розмір|размер|зріст|\bріст\b|\bрост\b|\bваг[ауи]\b|\bвес\b", text):
        found.discard("sizing")
    if re.search(r"коли|когда", text) and not re.search(r"відправили|отправили|відправлено|отправлено|ттн|накладн|трек", text):
        found.discard("shipment_status")
    if re.search(r"(?:вже|уже).{0,35}(?:катиш|катыш|катик|кошлат|порвал|зліз|злез)|після\s+прання.{0,20}(?:з'явил|появил|поліз|виліз)", text):
        found.add("complaint")
    if re.search(r"реквіз|реквиз|квитанц|квитанці|фото|замір|замер|рахун", text) and not re.search(r"товар|посил|посыл|замовлен|заказ", text):
        found.discard("dispatch")
    if re.fullmatch(r"(?:я\s+)?(?:передумав|передумала|передумали)[.! ]*", text):
        found.add("cancel")
    if re.search(r"\b(?:давай|давайте|беру|візьму|возьму)\b", text) and (requested_size(text) or requested_color(text) or requested_quantity(text)):
        found.add("order")
    if re.search(r'\b(?:хочу|хотів|хотіла|хотел|хотела|бажаю)\b', text) and (requested_size(text) or requested_color(text)) and not re.search(r'не\s+(?:хочу|бажаю)', text):
        found.add('order')
    if re.search(r'\b(?:можна|можно|давайте|домовимось|договоримся)\s+за\s+\d+\s*(?:грн|гривень|uah|₴)?[?!., ]*$', text):
        found.add('discount')
    if re.fullmatch(r'нова\s+пропозиція:\s*\d+(?:[.,]\d+)?\s*грн[.! ]*', text):
        found.add('discount')
    if re.fullmatch(r'(?:(?:так|да|добре|ок|окей|yes|okay|ну|бля(?:ть|дь)?|сука)\s*[, ]*\s*)*(?:хочу|купити|купить|беру|візьму|возьму|замовляю|закажу|куплю|оформити|оформить)[.! ]*',text):
        found.add('order')
    return found


def reports_payment(text):
    text = canonical(text)
    pattern = r"\b(?:оплатив|оплатила|сплатив|сплатила|перерахував|перерахувала|(?:скинув|скинула)\s+(?:кошти|гроші|деньги|оплату))\b"
    return any(not re.search(r"\bне\s*$", text[max(0, match.start() - 15):match.start()])
               for match in re.finditer(pattern, text))


