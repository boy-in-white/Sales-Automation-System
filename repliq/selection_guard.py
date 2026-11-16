"""Clarify invalid or ambiguous selections before they can change an order."""

import copy
import re

from catalog import COLORS, SIZES
from customer_language import requested_sizes, requested_colors, requested_quantity, parameters
from customer_phrasing import canonical
from inventory import StockUnavailable, stock_rows, stock_signature
from size_advice import asks_size_advice
from order_summary import order_items
from shop_facts import measurements_reply


def selected_product(catalog, saved, advert):
    cart = saved.get('cart') or []
    keys = {item.get('product') for item in cart}
    key = next(iter(keys)) if len(keys) == 1 else saved.get('product') if not cart else None
    return catalog.products.get(key or catalog.adverts.get(str(advert)))


def guard_selection(catalog, inventory, raw, saved, advert, flags, has_attachments=False):
    text = canonical(raw)
    if has_attachments or len(text) > 6000 or flags.intersection({'cancel', 'complaint', 'returns'}):
        return None
    explicit = catalog.explicit_products(text)
    product = explicit[0] if len(explicit) == 1 else selected_product(catalog, saved, advert)
    context = copy.deepcopy(saved)

    def reply(body, awaiting=None, guard=None, human=False):
        sent = {}
        if awaiting:
            sent['awaiting'] = awaiting
        if human:
            sent['handoff_now'] = True
        return {'text': body, 'context': context, 'on_send': sent, 'guard': guard or {}, 'notes': []}

    quantity = requested_quantity(text)
    body, _ = parameters(text, saved, weight_for_fit=False)
    if 'height' in body and 'weight' in body:
        return None
    raw_quantities = list(re.finditer(r'(?<![\w.,])([-−]?\d+(?:[.,]\d+)?)\s*(?:шт\b|штук\w*|пар[аиу]?\b)', text))
    if not raw_quantities:
        raw_quantities = list(re.finditer(r'\b(?:хочу|беру|візьму|возьму|давай|давайте|додай|добав)\s+(?:(?:ще|замовити|купити)\s+)?([-−]?\d+(?:[.,]\d+)?)\b(?!\s*(?:грн|грив|uah|₴|варіант|вариант|спосіб|способ))', text))
    invalid_numeric = any(len(match[1]) > 6 or not match[1].isdigit() or not 1 <= int(match[1]) <= 20 for match in raw_quantities)
    if invalid_numeric or quantity is not None and not 1 <= quantity <= 20:
        return reply('Підкажіть кількість цілим числом від 1 до 20, будь ласка. Попередній вибір збережено.', saved.get('awaiting') or 'quantity')
    # Personal data and addresses are interpreted by the delivery parser, not as variants.
    if re.search(r'\d{7,}|\b(?:телефон|відділення|поштомат|отделение|область|піб|получатель)\s*[:№#]', text):
        return None
    if len(explicit) > 1:
        return None  # Valid multi-product baskets have their own per-line clarification.
    if not product:
        return None
    # Historical adverts supply names to recognize, never specifications or stock.
    # Generic "pants" must not turn a request for another brand into Suvene.
    brands = (r'broken\s+planet|brocken\s+planet|брокен\s+планет', r'trapstar|трапстар',
              r'amiri|амірі|амири', r'lanvin|ланвін|ланвин', r'off[ -]?white|офф?\s+вайт',
              r'burberry|барбері|барбери', r'represent', r'iphone|айфон')
    target_name = canonical(product['name'] + ' ' + ' '.join(product['aliases']))
    different_brands = [pattern for pattern in brands if re.search(r'\b(?:' + pattern + r')\b', text) and
                        not re.search(r'\b(?:' + pattern + r')\b', target_name)]
    if different_brands and not re.search(r'порівн|сравн|схож|подіб|похож|замість|вместо', text):
        return reply('У цьому оголошенні — %s. Щодо іншої марки уточню наявність окремо, щоб не переплутати товари.' % product['name'], human=True)
    # A shared brand name is not proof that a hoodie is the advertised pair of pants.
    families = [('hoodie', r'кофт\w*|худі|худи|зіпк\w*|зипк\w*'), ('shorts', r'шорт\w*'),
                ('jacket', r'куртк\w*|пухов\w*'), ('tee', r'футбол\w*|майк\w*'), ('pants', r'штан\w*|брюк\w*')]
    asked = [(key, pattern) for key, pattern in families if re.search(r'\b(?:' + pattern + r')\b', text)]
    other = [pattern for key, pattern in asked if key != product.get('kind') and
             not re.search(r'\b(?:' + pattern + r')\b', canonical(product['name']))]
    if len(asked) == 1 and other and not re.search(r'як\s+у|как\s+у|порівн|сравн|замість|вместо|довжин|замір|замер|ширин', text):
        return reply('У цьому оголошенні — %s. Щодо іншої речі уточню у власника, щоб не переплутати товари.' % product['name'], human=True)

    unsupported = re.search(r'(?<!\w)(?:[3-9]xl|xxxl|xxxxl|xxs|xxxs|[2-9]xs)(?!\w)|(?:розмір|размер)\s*[:—-]?\s*\d{2,3}\b', text)
    if unsupported and not flags.intersection({'measurements'}) and not asks_size_advice(text, saved.get('awaiting')):
        codes = [s for s in SIZES if s in product.get('garment_measurements', {}) and s not in product.get('excluded_sizes', [])]
        codes = codes or [r['size'] for r in product.get('sizes', []) if r['size'] not in product.get('excluded_sizes', [])]
        return reply('Для цієї моделі використовуємо розміри %s. Який бажаєте? Якщо потрібна порада — допоможу підібрати.' % ', '.join(codes) if codes else
                     'Такого розміру в каталозі цієї моделі немає. Який звичний розмір ви носите?', 'size')
    sizes, colors = requested_sizes(text), requested_colors(text)
    if len(sizes) < 2 and len(colors) < 2:
        return None
    if len(sizes) > 1 and 'measurements' in flags and 'order' not in flags:
        measured, missing = measurements_reply(product, sizes)
        if 'availability' not in flags:
            return reply('Ось заміри:\n' + measured, human=missing)
        try:
            rows = stock_rows(inventory, product)
        except StockUnavailable:
            return reply('Ось заміри:\n' + measured + '\nНаявність цих розмірів зараз уточню.', human=True)
        available = [size for size in sizes if size not in product.get('excluded_sizes', []) and
                     any(row['size_code'] == size and row['available'] > 0 for row in rows)]
        body = 'Ось заміри:\n' + measured
        body += '\n\nЗараз у наявності: ' + ', '.join(available) + '.' if available else '\n\nЦих розмірів зараз немає в наявності.'
        return reply(body, guard={'product': product['key'], 'stock': stock_signature(rows)}, human=missing)
    if 'order' in flags and not re.search(r'\b(?:чи|або|или)\b', text):
        from shopping_cart import parse_items
        items, ambiguous = parse_items(catalog, text, product['key'])
        if len(items) > 1 and not ambiguous:
            return None  # Explicit "M black and L grey" is a basket, not indecision.
    if (saved.get('cart') or quantity and quantity > 1 or re.search(r'обидв|обо[єе]|\bоба\b|кожн|кажд', text) or
            flags.intersection({'measurements', 'photos'}) or asks_size_advice(text, saved.get('awaiting'))):
        return None
    try:
        rows = stock_rows(inventory, product)
    except StockUnavailable:
        return reply('Зараз уточню наявність. Який саме розмір і колір вам потрібні?', 'size')
    wanted_sizes = sizes or ([saved['size']] if saved.get('size') else [])
    wanted_colors = colors or ([saved['color']] if saved.get('color') else [])
    amount = quantity or saved.get('quantity', 1)
    matches = [r for r in rows if r['available'] >= amount and
               (not wanted_sizes or r['size_code'] in wanted_sizes) and (not wanted_colors or r['color_code'] in wanted_colors)]
    if not matches:
        matches = [r for r in rows if r['available'] >= amount and
                   (not saved.get('size') or r['size_code'] == saved['size'])]
    options = list(dict.fromkeys(r['size_code'] + ' · ' + COLORS.get(r['color_code'], r['color_code']) for r in matches))[:8]
    context.pop('pending_alternatives', None)
    body = ('Зараз є: ' + '; '.join(options) + '. ') if options else 'Потрібні варіанти зараз не підтверджені в наявності. '
    if len(colors) > 1 and len(sizes) < 2 and saved.get('size') and matches:
        labels = list(dict.fromkeys(COLORS.get(r['color_code'], r['color_code']) for r in matches))
        body = 'У розмірі %s зараз є: %s. ' % (saved['size'], ', '.join(labels))
    body += ('Який варіант обираєте?' if re.search(r'\b(?:чи|или|або)\b|\s[/−-]\s', text) else
             'Вам одну річ чи кілька? Якщо одну — який саме розмір і колір обираєте?')
    return reply(body, 'size' if len(sizes) > 1 else 'color', {'product': product['key'], 'stock': stock_signature(rows)})


def slot_hint(catalog, inventory, saved, advert):
    """An unrecognized slot answer gets a relevant hint, not a generic sales reset."""
    awaiting = saved.get('awaiting')
    if awaiting not in ('size', 'color'):
        return None
    product = selected_product(catalog, saved, advert)
    if not product:
        return None
    try:
        rows = stock_rows(inventory, product)
    except StockUnavailable:
        return None
    available = [r for r in rows if r['available'] >= saved.get('quantity', 1) and
                 (awaiting != 'color' or not saved.get('size') or r['size_code'] == saved['size'])]
    if not available:
        return None
    if awaiting == 'color':
        choices = list(dict.fromkeys(COLORS.get(r['color_code'], r['color_code']) for r in available))
        body = 'У розмірі %s зараз є: %s. Який із цих кольорів обираєте?' % (saved.get('size') or 'обраному', ', '.join(choices))
    else:
        choices = sorted({r['size_code'] for r in available}, key=lambda s: SIZES.index(s) if s in SIZES else 99)
        body = 'Зараз є розміри %s. Напишіть, який обираєте, або запитайте — допоможу підібрати.' % ', '.join(choices)
    return {'text': body, 'context': copy.deepcopy(saved), 'on_send': {'awaiting': awaiting},
            'guard': {'product': product['key'], 'stock': stock_signature(rows)}, 'notes': []}


def invoice_variant(catalog, inventory, text, saved, flags, has_attachments=False):
    """Stock questions do not edit a basket or become delivery details."""
    basket_question = bool(saved.get('cart') and 'availability' in flags and 'order' not in flags)
    if not (saved.get('invoice_issued') or basket_question) or has_attachments:
        return None
    sizes, colors = requested_sizes(text), requested_colors(text)
    if not (sizes or colors) or flags.intersection({'measurements', 'photos', 'material', 'description', 'returns', 'complaint', 'cancel'}):
        return None
    if asks_size_advice(text, saved.get('awaiting')) or re.search(r'\b(?:телефон\w*|місто|город|село|піб|фіо|фио|адрес\w*|відділен\w*|поштомат\w*)\b|\d{7,}', text):
        return None
    items = order_items(catalog, saved)
    named = catalog.explicit_products(text)
    keys = {item['product'] for item in items}
    key = named[0]['key'] if len(named) == 1 else next(iter(keys)) if len(keys) == 1 else None
    matches = [(i, item) for i, item in enumerate(items) if item['product'] == key]
    context = copy.deepcopy(saved)

    def reply(body, guard=None, human=False):
        return {'text': body, 'context': context, 'on_send': {'handoff_now': True} if human else {},
                'guard': guard or {}, 'notes': []}

    if len(matches) != 1 or key not in catalog.products:
        if len(named) == 1 and key in catalog.products and re.search(r'\?|\bє\b|есть|наявн|налич', text):
            product = catalog.products[key]
            size, color, amount = sizes[-1] if sizes else None, colors[-1] if colors else None, requested_quantity(text) or 1
            try:
                rows = stock_rows(inventory, product)
            except StockUnavailable:
                return reply('Зараз уточню наявність цієї речі. Уже оформлене замовлення збережено.')
            available = any(row['available'] >= amount and (not size or row['size_code'] == size) and
                            (not color or row['color_code'] == color) for row in rows)
            return reply(('Так, цей варіант є в наявності.' if available else 'Цього варіанта у потрібній кількості зараз немає.') +
                         ' Ваше оформлене замовлення залишаю без змін.', {'stocks': {key: stock_signature(rows)}})
        return reply('Для якої речі перевірити цей варіант? Напишіть назву й розмір або колір. Ваше замовлення збережено.')
    _, item = matches[0]
    size, color, amount = (sizes[-1] if sizes else item.get('size'), colors[-1] if colors else item.get('color'),
                           requested_quantity(text) or item['quantity'])
    try:
        rows = stock_rows(inventory, catalog.products[key])
    except StockUnavailable:
        return reply('Зараз не можу підтвердити інший варіант. Поточне замовлення залишаю без змін.')
    guard = {'stocks': {key: stock_signature(rows)}}
    available = any((not size or r['size_code'] == size) and (not color or r['color_code'] == color) and
                    r['available'] >= amount for r in rows)
    if not available:
        labels = list(dict.fromkeys(COLORS.get(r['color_code'], r['color_code']) for r in rows if r['size_code'] == size and r['available'] >= amount))
        body = 'Цього варіанта у потрібній кількості зараз немає.'
        if labels:
            body += ' У розмірі %s є: %s.' % (size, ', '.join(labels))
        return reply(body + ' Ваш попередній вибір збережено без змін.', guard)
    different = size != item.get('size') or color != item.get('color')
    requested_change = bool('order' in flags or re.search(r'змін|замін|помін|помен|хочу|інший|другой', text))
    if different and requested_change:
        return reply('Цей варіант є в наявності. Узгодимо зміну вже оформленого замовлення перед відправкою. Повторно переказувати кошти не потрібно.', guard, True)
    return reply('Так, цей варіант є в наявності. Ваш попередній вибір залишаю без змін.', guard)
