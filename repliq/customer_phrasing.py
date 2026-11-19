"""Small, explicit spelling vocabulary mined from buyer questions, not seller replies.

Only parser text is normalized. Addresses, names and original messages are kept
unchanged. No fuzzy guessing of prices, payment status, stock or product facts.
"""

import re
from catalog import normalize

WORDS = {
    'розмер': 'розмір', 'розмир': 'розмір', 'розімр': 'розмір', 'розмрі': 'розмір',
    'размир': 'размер', 'размєр': 'розмір', 'прозмір': 'розмір', 'зпіст': 'зріст',
    'добрго': 'доброго', 'дорбого': 'доброго', 'добрйи': 'добрий',
    'віправка': 'відправка', 'відпрявка': 'відправка', 'отпрака': 'отправка',
    'віправите': 'відправите', 'відпрявите': 'відправите', 'віправити': 'відправити',
    'доскавкою': 'доставкою', 'хамовити': 'замовити', 'хамоаити': 'замовити',
    'замивтит': 'замовити', 'замовтити': 'замовити', 'замвитити': 'замовити',
    'дредоплаті': 'передоплаті', 'предплаті': 'передоплаті', 'кариту': 'карту',
    'черниые': 'черные', 'черних': 'черных', 'черні': 'чорні',
    'кольру': 'кольору', 'коляру': 'кольору', 'виьором': 'вибором',
    'ска': 's', 'ску': 's', 'еску': 's', 'эску': 's', 'еска': 's', 'эска': 's',
    'емку': 'm', 'эмку': 'm', 'мку': 'm', 'лку': 'l', 'ельку': 'l', 'эльку': 'l',
    'хl': 'xl', 'xл': 'xl', 'xхl': 'xxl', 'ххl': 'xxl',
    'блек':'black', 'блэк':'black', 'блєк':'black', 'blak':'black',
    'вайт':'white', 'уайт':'white', 'вайтова':'white', 'грей':'gray', 'грэй':'gray',
    'блю':'blue', 'пінк':'pink', 'пинк':'pink', 'ред':'red', 'беж':'beige', 'бордо':'burgundy',
    'замовлити':'замовити', 'замовть':'замовити', 'закзать':'заказать', 'закааз':'заказ',
    'паслеплата':'післяплата', 'послеплотой':'післяплатою', 'наложкй':'наложкою',
    'навності': 'наявності', 'нявності': 'наявності', 'олкс': 'олх',
    'хочукупити':'хочу купити', 'хочузамовити':'хочу замовити', 'белий':'білий', 'бєлий':'білий',
}


def canonical(text):
    text = normalize(re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]', '', str(text)))
    text = re.sub(r'\b[^\W\d_]+\b', lambda m: WORDS.get(m[0], m[0]), text)
    text = re.sub(r'\bвідпрявля', 'відправля', text)
    text = re.sub(r'\b(?:після|после)[ -]+(?:оплат|плат)', 'післяплат', text)
    text = re.sub(r'\b(?:перед|пред)[ -]+(?:оплат|плат)', 'передоплат', text)
    text = re.sub(r'\bна\s+лож[её]?[нн]?[ыи]м\s+плат', 'наложним плат', text)
    return text


def cod_mentioned(text):
    text = canonical(text)
    return bool(re.search(r'налож|післяплат|послеплат|накладен|накладн(?:им|ий|ою|ым|ой|ого)|'
                          r'оплат\w*\s+(?:у|в|на|при|після|после)\s+(?:відділен|отделен|отриман|получен|пошті|почте)', text))


def wants_dispatch_notice(text):
    text = canonical(text)
    if re.search(r'\b(?:не\s+(?:треба|потрібно|надо|нужно|пиш|повідом|сообщ)|без\s+сповіщ)', text):
        return False
    when = r'(?:як(?:\s+тільки)?|как(?:\s+только)?|коли|когда|після|после)'
    dispatch = r'(?:відправ(?:ите|лять|лення|или|лено)|отправ(?:ите|ят|ления|или|лено))'
    notice = r'(?:напиш(?:іть|и|ите)|повідом(?:те|іть)|сообщ(?:ите|и)|дайте\s+знати|сповістіть|дайте\s+знать)'
    event = r'\b' + when + r'\s+(?:(?:вже|уже|ви|вы)\s+){0,2}' + dispatch + r'\b'
    return bool(re.search(event + r'.{0,55}\b' + notice + r'\b|\b' + notice + r'\b.{0,40}' + event, text))


def platform_notice(text):
    """Exact OLX notification templates, not an ordinary customer's quoted text."""
    text = normalize(text)
    return bool(re.match(r'^у вас придбали товар з olx доставкою!\s*підтвердьте купівлю', text) or
                re.match(r'^у вас купили товар с olx доставкой!\s*подтвердите', text))


def cod_deposit_question(text, context=None):
    text = canonical(text)
    deposit = r'(?:передоплат\w*|передплат\w*|предоплат\w*|\b100\b|\bсто\s+грив\w*|\bпп\b)'
    without = list(re.finditer(r'\bбез\s+(?:(?:жодн\w*|никак\w*)\s+)?' + deposit, text))
    if any(not re.search(r'(?:\bне|не\s+прошу|не\s+хочу)\s*$', text[max(0,m.start()-20):m.start()]) for m in without):
        return 'without'
    if re.search(r'\bне\s+(?:(?:буду|хочу)\s+(?:робити\s+)?)?(?:передоплач|передоплат|передплат|предоплат)', text):
        return 'without'
    why = r'(?:для\s+чого|для\s+чего|навіщо|нащо|зачем|чому|почему|яка\s+мета|какой\s+смысл|що\s+покрива)'
    asking = bool(re.search(why + r'.{0,60}' + deposit + '|' + deposit + r'.{0,60}' + why, text))
    bare_why = bool(re.fullmatch(r'(?:а\s+)?' + why + r'[?!. ]*', text))
    method = (context or {}).get('checkout_method') or (context or {}).get('pending_checkout_method')
    if (asking and (method not in ('full','olx') or re.search(r'\b100\b', text))) or bare_why and method == 'cod':
        return 'purpose'
    return None


GREETINGS = (
    (r'добрий\s+ранок', 'Добрий ранок!'),
    (r'доброго\s+ранку|доброе\s+утро', 'Доброго ранку!'),
    (r'добрий\s+вечір|добрый\s+вечер|добрий\s+вечер', 'Добрий вечір!'),
    (r'доброго\s+вечора', 'Доброго вечора!'),
    (r'доброї\s+ночі|доброй\s+ночи', 'Доброї ночі!'),
    (r'добрий\s+день|добрый\s+день', 'Добрий день!'),
    (r'доброго\s+дня', 'Доброго дня!'),
    (r'прив[іе]т(?:ик|ики)?|hello|hi', 'Привіт!'),
    (r'сал[ая]м(?:\s+алейкум)?|салют', 'Салам!'),
    (r'вітаю|здравствуй(?:те)?', 'Вітаю!'),
)


def customer_greeting(text):
    text = canonical(text)
    found = [(match.start(), answer) for pattern, answer in GREETINGS
             for match in re.finditer(r'\b(?:' + pattern + r')\b', text)]
    return max(found)[1] if found else None


def greeting_only(text):
    text = canonical(text)
    return any(re.fullmatch(r'(?:' + pattern + r')[^\w]*', text) for pattern, _ in GREETINGS)
