"""Extract contact fields independently; conversational captions are not address data."""

import copy
import re

from catalog import normalize

NAME = re.compile(r"[A-Za-zА-Яа-яІіЇїЄєҐґЁё'’ʼ-]+(?:\s+[A-Za-zА-Яа-яІіЇїЄєҐґЁё'’ʼ-]+){1,3}")
CHAT = re.compile(r"^(?:ось\b|вот\b|от\s|це\s|это\s|привіт\b|привет\b|добрий\s+(?:день|вечір|ранок)|доброго\s+(?:дня|вечора|ранку)|добрый\s+(?:день|вечер)|дякую\b|спасибо\b|я\s|ми\s|давай|хочу|передум|оплат|сплат|надсил|надіс|скин|підкаж|помож|допом|поклич|перевір|зрозум|поняв|ну\b|бля(?:ть|дь)?\b|будь[-\s]+ласка\b|пожалуйста\b)|квитанц|квитанці|\bчек\b|[?]|https?://", re.I)
DISCOURSE = re.compile(r'^(?:не\b|ні\b|нет\b|так\b|ага\b|ок(?:ей)?\b|що\b|шо\b|чому\b|який\b|яка\b|коли\b|как\b|почему\b|зач[её]м\b)|\b(?:хуй|нахуй|лох|дебіл|дебил|ідіот|идиот|сука)\b|в\s+рот\s+(?:[єїе]бав|[єїе]бал)',re.I)
LABELS = {
    "name": r"(?:піб(?:\s+отримувача)?|ф[іи]о|отримувач|получатель)",
    "phone": r"(?:номер\s+телефону|телефон|тел\.?|phone)",
    "place": r"(?:місто(?:\s*(?:або|/)\s*село)?(?:,?\s*область)?|город|населений\s+пункт|село|адреса|адрес)",
    "branch": r"(?:відділення\s+або\s+поштомат\s+нп,?\s*номер|номер\s+(?:відділення|поштомата)(?:\s*\([^)]*\))?)",
}

# Recipient fields must describe a person or a destination, never a promise,
# acknowledgment, question or an instruction to the assistant. Case is not a
# signal: customers legitimately write their names entirely in lower case.
CONVERSATION = re.compile(
    r'^(?:добре|хорошо|ладно|гаразд|зараз|сейчас|секунд\w*|хвилин\w*|минут\w*|'
    r'одну\s+(?:хвилин\w*|минут\w*)|зачека\w*|почека\w*|подожди\w*|чекай\w*|'
    r'готово|згоден|згодна|зрозум\w*|понял\w*|вже|уже|ще|еще|пізніше|потім|потом|'
    r'де|куди|скільки|чи|можна|можно|буде|будуть|сколько|кто|куда|где|'
    r'заміни\w*|замініть|змініть|поменя\w*|замени\w*)\b|'
    r'\b(?:зроблю|сделаю|заповню|заполню|скину|надішлю|пришлю|відправлю|отправлю|'
    r'зачекайте|підтверджую|оформлюйте|перепрошую)\b', re.I)
STREET_PART = r'(?:вул(?:иця|иці|ицю)?\.|вулиц[яію]\s+|ул(?:ица|ице|ицу)?\.|улиц[ауы]\s+|проспект\w*\s+|провулок\s+|переулок\s+)'
STREET = re.compile(r'^' + STREET_PART, re.I)
KIND = r'(?:відділен[а-яіїєґё]*|отделен[а-яіїєґё]*|поштомат[а-яіїєґё]*|почтомат[а-яіїєґё]*|від\.|отд\.)'
CARRIER = r'(?:нова\s+пошта|новая\s+почта|нп)'
MARKER = r'(?:номер\s*:?[ \t]*|no\.?\s*|[nн]\s*[*.°º]?\s*|[№#:]\s*)'
BRANCH_TITLE = r'(?:поштове\s+|почтовое\s+)?' + KIND + r'(?:\s+(?:нп|нової\s+пошти|новой\s+почты))?'
POINT = (r'\b(?:' + CARRIER + r'\s*[,.:—-]?\s*)?' + BRANCH_TITLE + r'\s*(?:на\s+)?' +
         r'(?:' + MARKER + r')?\d{1,6}\b(?:\s+нп)?|\b' + CARRIER + r'\s*(?:' + MARKER + r')?\d{1,6}\b|'
         r'\b\d{1,6}\s*[-—]?\s*' + BRANCH_TITLE)
PLACE_PREFIX = r'(?:м\.|с\.|м\s+|с\s+|смт\.?\s*|місто\s+|город\s+|село\s+|селище\s+)'
AREA_PART = r'[А-Яа-яІіЇїЄєҐґЁё-]+\s+(?:обл(?:асть|асті)?\.?|район(?:у)?|р-н)'
PLACE_TYPES = {'м': 'місто', 'місто': 'місто', 'город': 'місто',
               'с': 'село', 'село': 'село', 'смт': 'селище', 'селище': 'селище'}
PHONE_SPAN = re.compile(r'(?<![\d+])(?:\+?38)?0(?:[ ().-]*\d){9}(?!\d)')
INLINE_LABEL = r'\s+(?=(?:' + '|'.join(LABELS.values()) + r')\s*[:—])'
SIDEBAR = re.compile(r'\b(?:передоплат\w*|оплат\w*|предоплат\w*|перед\s+плат\w*|переказ\w*|'
                     r'замовлен\w*|отправл\w*|квитанц\w*|скинув|скинула|сума|сумма|ттн|'
                     r'тканин\w*|карта|картк\w*|доставк\w*|замір\w*|розмір\w*)\b', re.I)


def conversational(value):
    value = normalize(value)
    return bool(CHAT.search(value) or DISCOURSE.search(value) or CONVERSATION.search(value) or confirms(value))


def waiting_acknowledgment(raw):
    """A buyer taking time is not failing to supply a field or confirming payment."""
    text = normalize(raw)
    words = re.findall(r"[\w'’]+", text)
    allowed = r'(?:добре|хорошо|ладно|гаразд|ок|окей|так|да|ага|зараз|сейчас|все|усе|всё|всьо|'
    allowed += r'я|вам|вже|уже|ще|еще|трохи|трошки|одну|одна|будь|ласка|пожалуйста|дякую|спасибо|'
    allowed += r'зроблю|сделаю|заповню|заполню|скину|скидаю|надішлю|пришлю|відправлю|отправлю|шукаю|ищу|'
    allowed += r'секунд\w*|хвилин\w*|минут\w*|зачека\w*|почека\w*|подожди\w*|чекай\w*|пізніше|потім|потом|згодом|'
    allowed += r'і|и|та|дані|данні|данные|квитанц\w*|чек|скр[іи]н|скр[іи]ншот|фото)'
    return bool(words and '?' not in text and len(text) <= 160 and
                all(re.fullmatch(allowed, word) for word in words) and
                re.search(r'зараз|сейчас|секунд|хвилин|минут|зачека|почека|подожди|чекай|пізніше|потім|потом|згодом|скину|надішлю|пришлю', text))


def clean_place(value):
    value = str(value).strip(' ,;')
    point = re.search(POINT, value, re.I)
    if point:
        value = value[:point.start()].strip(' ,;')
    street = re.search(r'(?<=[,\s])' + STREET_PART, value, re.I)
    if street:
        value = value[:street.start()].rstrip(' ,;')
    leading_area = re.match(r'^(' + AREA_PART + r')[, ]+(' + PLACE_PREFIX + r'.+)$', value, re.I)
    if leading_area:
        value = leading_area[2].rstrip('.') + ', ' + leading_area[1]
    value = re.sub(r'^' + PLACE_PREFIX + r'\s*', '', value, flags=re.I)
    value = re.sub(r'(?<=[^\s,])\s+(' + AREA_PART + r')(?=$|[,\s])', r', \1', value, flags=re.I).strip(' ,')
    return value


def locality_type(value):
    value = re.sub(r'^' + AREA_PART + r'[, ]+', '', value.strip(), flags=re.I)
    match = re.match(PLACE_PREFIX, value, re.I)
    return PLACE_TYPES.get(match[0].casefold().strip(' .')) if match else None


def valid_place(value):
    return bool(3 <= len(value) <= 200 and re.search(r'[A-Za-zА-Яа-яІіЇїЄєҐґ]', value) and
                not conversational(value) and not STREET.search(value) and not SIDEBAR.search(value))


def confirms(text):
    text = normalize(text)
    if "?" in text or re.search(r"\d", text) or len(text) > 180:
        return False
    if re.fullmatch(r'[+👍✅☑️✔️\s.!]+', text):
        return True
    words = re.findall(r"[\w'’]+", text)
    strong = {"так", "да", "ага", "угу", "добре", "добро", "ок", "окей", "ok", "okay", "вірно", "верно", "правильно",
              "правильні", "вірні", "верные", "коректно", "підтверджую", "подтверждаю", "підтверджено", "підходить",
              "підходе", "підходить", "подходит", "чудово", "супер", "оформлюйте", "оформляйте", "відправляйте", "отправляйте"}
    fillers = {"все", "усе", "всё", "всьо", "всі", "усі", "дані", "данные", "дуже", "дякую", "спасибо", "будь", "ласка", "пожалуйста", "в", "порядку", "вам"}
    return bool(words and set(words).intersection(strong) and all(word in strong | fillers for word in words))


def branch_value(value):
    # NFKC expands the numero sign to "No"; preserve it as an address marker.
    value = normalize(str(value).replace("№", "#")).strip(" .,:;")
    value = re.sub(r'^(?:заміни|замініть|змініть|поміняй|поменяйте|замени|нове|новий|інше|інший)\s+', '', value)
    value = re.sub(r'^(?:на|до|в|у)\s+', '', value)
    value = re.sub(r"^" + CARRIER + r"\s*[,.:—-]?\s*", "", value)
    value = re.sub(MARKER + r'(?=\d)', '#', value, flags=re.I)
    patterns = (r"^" + BRANCH_TITLE + r"\s*(?:на\s+)?[:#—-]?\s*(\d{1,6})\s*(?:нп)?$",
                r"^[#:]?\s*(\d{1,6})\s*[-—]?\s*" + BRANCH_TITLE + r"$",
                r"^[#:]?\s*(\d{1,6})$")
    for pattern in patterns:
        match = re.fullmatch(pattern, value, re.I)
        if match:
            return ("Поштомат №" if re.search(r"поштомат|почтомат", value) else "Відділення №") + match[1]
    return None


def valid_name(value):
    return bool(NAME.fullmatch(value) and not conversational(value) and
                all(len(word) > 1 for word in value.split()) and
                not re.search(r'^(?:не\s+(?:знаю|хочу|можу|могу|буду|пам.?ятаю|помню)|немає\b|нет\s+данн)', normalize(value)) and
                not re.search(r"област[ьіи]|\bобл\b|район|поштомат|відділен|отделен|нова\s+пошта|новая\s+почта", value, re.I) and
                not re.match(PLACE_PREFIX, value, re.I) and not STREET.search(value) and not SIDEBAR.search(value))


def contact_segments(raw):
    return re.split(r'[\n;|]+|(?<=[.!?])\s+(?=(?:а\s+|і\s+)?(?:чи|як|яки[йх]|яка|яке|коли|скільки|можна|можно|како[йея]|как|когда|сколько|на\s+(?:зиму|літо))\b)', raw, flags=re.I)


def contact_details(raw, saved, awaiting=None):
    result, values = copy.deepcopy(saved), {}
    # Repair only self-evident parser pollution from previous releases. Never
    # reconstruct a payment, name or phone from another conversation or seller.
    if result.get('name') and not valid_name(result['name']):
        result.pop('name', None)
    if result.get('place'):
        embedded = re.search(POINT, result['place'], re.I)
        if embedded and not result.get('branch'):
            point = branch_value(embedded[0])
            if point:
                result['branch'] = point
        place = clean_place(result['place'])
        if valid_place(place):
            result['place'] = place
        else:
            result.pop('place', None)
            result.pop('place_type', None)
    # Accept a pasted form, semicolon-separated fields or one natural line.
    lines = []
    candidates = [part for segment in contact_segments(raw) for part in re.split(INLINE_LABEL, segment, flags=re.I)]
    for line in candidates:
        if '?' in line:
            continue
        # Extract a complete phone first, so an adjacent house or branch number
        # cannot make a valid phone appear too long.
        phone = PHONE_SPAN.search(line)
        if not phone:
            phone = re.search(r'(?<![\d+])(?:\+?38)?0(?:[ ().-]*\d){7,10}(?!\d)', line)
        if phone and re.search(r"[А-Яа-яІіЇїЄєҐґA-Za-z]", line) and not SIDEBAR.search(line[:phone.start()]):
            before = line[:phone.start()].strip(" ,:")
            after = line[phone.end():].strip(" ,:")
            before = re.sub(r"^(?:ось|вот)\s+(?:квитанц[а-яіїєґё]*|(?:мої\s+)?дані)\s*[,.:—-]\s*", "", before, flags=re.I)
            before = re.sub(r'(?:^|[,\s])' + LABELS['phone'] + r'\s*(?:на)?$', '', before, flags=re.I).strip(' ,:')
            if before and not conversational(before):
                lines.append(before)
            lines.append(phone[0])
            line = after
        point = re.search(POINT, line, re.I)
        if point and not conversational(line) and not re.match(r"^(?:номер|відділення\s+або)\b", line.strip(), re.I):
            before = line[:point.start()].strip(' ,:')
            if before:
                lines.append(before)
            lines.append(point[0])
            rest = line[point.end():].strip(" ,:")
            if rest:
                lines.append(rest)
        elif line.strip():
            lines.append(line)
    source = [(i, re.sub(r"^[1-4][.)]\s+", "", line.strip(" •\t"))) for i, line in enumerate(lines) if line.strip()]
    free, phone_line = [], None
    for index, line in source:
        line = re.sub(r'^(?:новий|нові|нове|інший|інша|новый|новая)\s+(?=телефон|номер|місто|город|відділення|поштомат)', '', line, flags=re.I)
        line = re.sub(r'^(?:заміни|замініть|змініть|поміняй|поменяйте|замени)\s+(?=' + '|'.join(LABELS.values()) + r')', '', line, flags=re.I)
        if any(re.fullmatch(label + r'\s*[:—-]?\s*', line, re.I) for label in LABELS.values()):
            continue  # Pasting an empty template must not become an address.
        labeled = False
        for field, label in LABELS.items():
            match = re.match(r"^" + label + r"\s*(?:[:—-]\s*|\s+)(.+)$", line, re.I)
            if match:
                value = re.sub(r'^на\s+', '', match[1].strip(), flags=re.I)
                if not re.fullmatch(r'[:—.\s_\-]*', value):
                    values[field] = value
                    if field == 'place':
                        label = line[:match.start(1)].casefold().strip(' :—-')
                        values['place_type'] = PLACE_TYPES.get(label) or locality_type(value)
                if field == "phone":
                    phone_line = index
                labeled = True
                break
        if labeled:
            continue
        digits = re.sub(r"[\s()+.-]", "", line)
        if re.fullmatch(r"\+?\d{7,15}", digits) and (digits.lstrip("+").startswith(("0", "380")) or awaiting == "phone"):
            values["phone"], phone_line = line, index
            continue
        point = branch_value(line)
        bare_pair = (len(source) == 2 and result.get('phone') and result.get('name') and not result.get('branch') and
                     any(i != index and valid_place(clean_place(other)) for i, other in source))
        if point and (re.search(r"від|отд|поштомат|почтомат|нп|пошта|почта|[№#]", line, re.I) or
                      awaiting == 'branch' or not result.get('branch') and len(source) >= 3 or bare_pair):
            values["branch"] = point
            continue
        if conversational(line) or STREET.search(line) or SIDEBAR.search(line) or line.endswith(":"):
            continue
        free.append((index, line))
    if "name" not in values:
        candidates = [(i, line) for i, line in free if valid_name(line) and
                      (awaiting == "name" or phone_line is None or i < phone_line)]
        if not candidates and phone_line is not None:
            candidates = [(i, line) for i, line in free if valid_name(line) and len(line.split()) >= 3]
        if candidates:
            index, value = candidates[0]
            # A single unlabelled city must not overwrite an already known name.
            if not result.get("name") or awaiting == "name" or phone_line is not None:
                values["name"] = value
                free = [(i, line) for i, line in free if i != index]
    if "place" not in values:
        places = [(i, line) for i, line in free if valid_place(clean_place(line))]
        if places and (not result.get("place") or awaiting == "place" or phone_line is not None):
            values["place"] = ", ".join(line for _, line in places)[:200]
    if "name" in values:
        if valid_name(values["name"]):
            result["name"] = values["name"]
    if "phone" in values:
        phone = re.sub(r"[\s()+.-]", "", values["phone"])
        if re.fullmatch(r"0\d{9}", phone):
            phone = "38" + phone
        if re.fullmatch(r"380\d{9}", phone):
            result["phone"] = "+" + phone
    if "place" in values:
        place = clean_place(values["place"])
        if valid_place(place):
            kind = values.get('place_type') or locality_type(values['place'])
            if kind:
                result['place_type'] = kind
            elif place != result.get('place'):
                result.pop('place_type', None)
            result["place"] = place
    if "branch" in values:
        point = branch_value(values["branch"])
        if point:
            result["branch"] = point
    if (result.get("place"), result.get('place_type')) != (saved.get("place"), saved.get('place_type')):
        for key in ("city_ref", "warehouse_ref", "branch_address"):
            result.pop(key, None)
    elif result.get("branch") != saved.get("branch"):
        for key in ("warehouse_ref", "branch_address"):
            result.pop(key, None)
    return result


def close_word(word, target, maximum=2):
    """Bounded edit distance including one adjacent transposition."""
    if abs(len(word) - len(target)) > maximum:
        return False
    previous, before = list(range(len(target) + 1)), None
    for i, char in enumerate(word, 1):
        current = [i]
        for j, other in enumerate(target, 1):
            cost = char != other
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if before is not None and i > 1 and j > 1 and char == target[j - 2] and word[i - 2] == other:
                value = min(value, before[j - 2] + 1)
            current.append(value)
        before, previous = previous, current
    return previous[-1] <= maximum
