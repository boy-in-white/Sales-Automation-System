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
    value = re.sub(r'((?:відділення|отделение|поштомат|почтомат))\s+на\s+', r'\1 ', value)
    value = re.sub(r"^(?:нова\s+пошта|новая\s+почта|нп)\s*[,.:—-]?\s*", "", value)
    kind = r"(?:відділен[а-яіїєґё]*|отделен[а-яіїєґё]*|поштомат[а-яіїєґё]*|почтомат[а-яіїєґё]*|від\.|отд\.|нп)"
    patterns = (r"^" + kind + r"\s*(?:нп\s*)?[:№#—-]?\s*(\d{1,6})\s*(?:нп)?$",
                r"^[№#:]?\s*(\d{1,6})\s*[-—]?\s*" + kind + r"(?:\s+нп)?$",
                r"^[№#:]?\s*(\d{1,6})$")
    for pattern in patterns:
        match = re.fullmatch(pattern, value, re.I)
        if match:
            return ("Поштомат №" if re.search(r"поштомат|почтомат", value) else "Відділення №") + match[1]
    return None


def valid_name(value):
    return bool(NAME.fullmatch(value) and not CHAT.search(normalize(value)) and not DISCOURSE.search(normalize(value)) and not confirms(value) and
                not re.search(r'^(?:не\s+(?:знаю|хочу|можу|могу|буду|пам.?ятаю|помню)|немає\b|нет\s+данн)', normalize(value)) and
                not re.search(r"область|\bобл\b|район|поштомат|відділен|отделен", value, re.I))


def contact_details(raw, saved, awaiting=None):
    result, values = copy.deepcopy(saved), {}
    # Accept a pasted form, semicolon-separated fields or one natural line.
    raw_lines = re.split(r"[\n;|]+", raw)
    lines = []
    point_pattern = (r"(?:відділен[а-яіїєґё]*|отделен[а-яіїєґё]*|поштомат[а-яіїєґё]*|почтомат[а-яіїєґё]*|нп)\s*[:№#-]?\s*\d{1,6}\b|"
                     r"\b\d{1,6}\s*(?:відділен[а-яіїєґё]*|отделен[а-яіїєґё]*|поштомат[а-яіїєґё]*|почтомат[а-яіїєґё]*)")
    for line in raw_lines:
        # Keep labelled fields together; the normal parser validates them below.
        phone = re.search(r"(?<!\d)(?:\+?38)?0(?:[ ().-]*\d){9}(?!\d)", line)
        if phone and re.search(r"[А-Яа-яІіЇїЄєҐґA-Za-z]", line) and not re.match(r"^(?:телефон|номер\s+телефону|тел[.:])", line.strip(), re.I):
            before = line[:phone.start()].strip(" ,:")
            after = line[phone.end():].strip(" ,:")
            before = re.sub(r"^(?:ось|вот)\s+(?:квитанц[а-яіїєґё]*|(?:мої\s+)?дані)\s*[,.:—-]\s*", "", before, flags=re.I)
            if before:
                lines.append(before)
            lines.append(phone[0])
            line = after
        point = re.search(point_pattern, line, re.I)
        if point and line[:point.start()].strip(" ,:") and not re.match(r"^(?:номер|відділення\s+або)\b", line.strip(), re.I):
            lines.append(line[:point.start()].strip(" ,:"))
            lines.append(point[0])
            rest = line[point.end():].strip(" ,:")
            if rest:
                lines.append(rest)
        elif line.strip():
            lines.append(line)
    source = [(i, re.sub(r"^[1-4][.)]\s+", "", line.strip(" •\t"))) for i, line in enumerate(lines) if line.strip()]
    free, phone_line = [], None
    for index, line in source:
        if '?' in line:
            continue  # A question about a field is not a correction to that field.
        line = re.sub(r'^(?:новий|нові|нове|інший|інша|новый|новая)\s+(?=телефон|номер|місто|город|відділення|поштомат)', '', line, flags=re.I)
        if any(re.fullmatch(label + r'\s*[:—-]?\s*', line, re.I) for label in LABELS.values()):
            continue  # Pasting an empty template must not become an address.
        labeled = False
        for field, label in LABELS.items():
            match = re.match(r"^" + label + r"\s*(?:[:—-]\s*|\s+)(.+)$", line, re.I)
            if match:
                value = match[1].strip()
                if not re.fullmatch(r'[:—.\s_\-]*', value):
                    values[field] = value
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
        if point and (re.search(r"від|отд|поштомат|почтомат|нп|пошта|почта|[№#]", line, re.I) or
                      awaiting == "branch" or len(source) >= 2 or all(result.get(k) for k in ("name", "phone", "place"))):
            values["branch"] = point
            continue
        if CHAT.search(normalize(line)) or DISCOURSE.search(normalize(line)) or confirms(line) or line.endswith(":"):
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
        places = [(i, line) for i, line in free if re.search(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]", line)]
        if places and (not result.get("place") or awaiting == "place" or phone_line is not None):
            values["place"] = ", ".join(line for _, line in places)[:200]
    if "name" in values:
        if valid_name(values["name"]):
            result["name"] = values["name"]
        else:
            result.pop("name", None)
    if "phone" in values:
        phone = re.sub(r"[\s()+.-]", "", values["phone"])
        if re.fullmatch(r"0\d{9}", phone):
            phone = "38" + phone
        if re.fullmatch(r"380\d{9}", phone):
            result["phone"] = "+" + phone
        else:
            result.pop("phone", None)
    if "place" in values:
        place = values["place"].strip()
        if (3 <= len(place) <= 200 and re.search(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]", place) and
                not CHAT.search(normalize(place)) and not DISCOURSE.search(normalize(place)) and not confirms(place) and
                not re.search(r'^(?:не\s+(?:знаю|хочу|можу|могу|буду|пам.?ятаю|помню)|немає\b|нет\s+данн)', normalize(place))):
            result["place"] = place
        else:
            result.pop("place", None)
    if "branch" in values:
        point = branch_value(values["branch"])
        if point:
            result["branch"] = point
        else:
            result.pop("branch", None)
    if result.get("place") != saved.get("place"):
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
