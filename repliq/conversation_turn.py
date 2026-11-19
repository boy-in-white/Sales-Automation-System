"""Bounded conversation memory and read-only answers around the order state machine.

Historical messages help interpret a question. Current catalog and stock remain
the only source of commercial facts. No network or language-model calls occur here.
"""

from functools import lru_cache
import copy
import json
from pathlib import Path
import re

from customer_language import intents, requested_size, requested_color, requested_quantity
from customer_phrasing import canonical
from customer_advice import FACT_TOPICS, fact_answers
from inventory import StockUnavailable, stock_rows, stock_signature
from selection_guard import selected_product
from shop_facts import measurements_reply, money

MEMORY_KEY = "conversation_memory"
MAX_TURNS = 6
INFORMATION = FACT_TOPICS | {"price", "measurements", "photos", "delivery", "payment", "dispatch", "arrival"}
TOPIC_HINTS = {"material": "склад тканини", "measurements": "заміри", "lining": "утеплення",
               "waist": "пояс", "fit": "посадка", "quality": "якість", "discount": "торг",
               "price": "ціна", "photos": "фото", "season": "сезонність", "pilling": "катишки"}


def safe_excerpt(text, limit=320):
    text = re.sub(r"https?://\S+", "[посилання]", str(text))
    text = re.sub(r"\bUA\d{20,}\b|(?<!\w)\+?\d[\d ()-]{7,}\d", "[дані]", text)
    return text.strip()[:limit]


def history_excerpt(history, source_ids):
    """Reuse already fetched history, excluding the current unanswered batch."""
    excluded = set(source_ids)
    return [{"speaker": "customer" if row.get("type") == "received" else "seller",
             "text": safe_excerpt(row.get("text") or "")}
            for row in history if row.get("_id") not in excluded and row.get("type") in ("received", "sent")][-8:]


def remember(saved, raw, answer, context, sent, interpreted=None):
    memory = saved.get(MEMORY_KEY, {})
    turns = list(memory.get("turns", []))[-MAX_TURNS + 1:]
    flags = sorted(intents(interpreted if interpreted is not None else raw).intersection(INFORMATION))
    turns.append({"customer": safe_excerpt(raw), "answer": safe_excerpt(answer),
                  "topics": flags, "product": context.get("product")})
    # Slots and invoice data remain in their existing validated representations.
    return {"version": 1, "turns": turns, "awaiting": sent.get("awaiting", context.get("awaiting"))}


def prepare(texts, saved, catalog=None, advert=None):
    """Resolve ellipsis only when a recent question establishes its meaning."""
    raw = "\n".join(str(value) for value in texts)
    text = canonical(raw)
    # A correction in a later bubble replaces the earlier choice. Newlines
    # alone must neither create extra items nor preserve a rejected variant.
    corrections = list(re.finditer(r'(?:^|\n)\s*(?:ні|нет|краще|лучше|замість|вместо|передумав|передумала)\b', raw, re.I))
    if corrections and not saved.get('cart') and not saved.get('invoice_issued') and not re.search(r'\b(?:ще|еще|додай|добав|обидв|обе|два|дві)\b', text):
        tail = raw[corrections[-1].start():].strip()
        if (requested_size(tail) or requested_color(tail)) and '?' not in tail and (not catalog or len(catalog.explicit_products(raw)) <= 1):
            selected = {}
            for key, parser in [('size',requested_size),('color',requested_color),('quantity',requested_quantity)]:
                selected[key] = parser(tail) or parser(raw[:corrections[-1].start()])
            from catalog import COLORS
            parts = [selected.get('size'), COLORS.get(selected.get('color'))]
            if selected.get('quantity') is not None:
                parts.append(str(selected['quantity']) + ' шт.')
            named = catalog.explicit_products(raw) if catalog else []
            return ['Хочу ' + (named[0]['name'] + ' ' if named else '') + ' '.join(str(v) for v in parts if v)]
    pending = saved.get('pending_subject_choice')
    if pending and catalog:
        named = catalog.explicit_products(text)
        if len(named) == 1 and named[0]['key'] in pending.get('products',[]) and not intents(text).intersection({'cancel','complaint'}):
            return [pending['text'], raw]
    turns = saved.get(MEMORY_KEY, {}).get("turns", [])
    previous = turns[-1] if turns else {}
    if re.fullmatch(r'[?？!\s]+', text):
        earlier = next((turn.get('customer', '') for turn in reversed(turns) if not re.fullmatch(r'[?？!\s]+', turn.get('customer', ''))), '')
        earlier_flags = intents(canonical(earlier))
        # Re-explain only a read-only fact question. Never replay a purchase,
        # payment, quantity change or shipping confirmation on a bare "?".
        if (earlier_flags.intersection(FACT_TOPICS | {'price', 'measurements'}) and
                not earlier_flags.intersection({'order', 'payment', 'cancel', 'returns', 'complaint'}) and
                not requested_color(earlier) and not requested_quantity(earlier)):
            return [earlier]
    if saved.get('recent_history'):
        # After a manual conversation, reuse questions from the OLX history that
        # was already fetched. Never recover prices, payments or order slots here.
        history = saved['recent_history'][-8:]
        customers = [row for row in history if row.get('speaker') == 'customer']
        if customers:
            previous = {'topics': sorted(intents(customers[-1].get('text', '')).intersection(INFORMATION))}
        if history and history[-1].get('speaker') == 'seller' and 'measurements' in intents(history[-1].get('text', '')):
            previous = {'topics': ['measurements']}
    flags = intents(text)
    # "Хочу розміри" immediately after measurements refers to the table,
    # not to another stock/color question. Resolve only from this dialogue.
    recent_measurements = any('measurements' in turn.get('topics', []) for turn in turns[-4:])
    recent_measurements |= any('measurements' in intents(row.get('text', '')) for row in saved.get('recent_history', [])[-6:] if row.get('speaker') == 'seller')
    if recent_measurements and re.fullmatch(r'(?:а\s+)?(?:хочу|потрібні|потрібен|можна|скиньте|скинь|надішліть)\s+(?:ці\s+|їхні\s+)?(?:розміри|размеры)[?!. ]*', text):
        return [raw + "\nПокажіть заміри виробу."]
    if recent_measurements and re.fullmatch(r'(?:ці|вони|эти|они|це)\s+(?:за?малі|малі|маленькие|замалі|завеликі|великі|большие)[.! ]*', text) and catalog:
        from catalog import SIZES
        product = selected_product(catalog, saved, advert)
        # Use only the sizes the client actually compared, never body numbers.
        compared = []
        for turn in reversed(turns[-4:]):
            if 'measurements' in turn.get('topics', []):
                from customer_language import requested_sizes
                compared = requested_sizes(turn.get('customer', ''))
                break
        compared = compared or ([saved['size']] if saved.get('size') else [])
        if product and compared:
            bigger = not re.search(r'завелик|велик|больш', text)
            edge = max(SIZES.index(v) for v in compared) if bigger else min(SIZES.index(v) for v in compared)
            next_index = edge + (1 if bigger else -1)
            if 0 <= next_index < len(SIZES) and SIZES[next_index] not in product.get('excluded_sizes', []):
                return ['Покажіть заміри ' + SIZES[next_index]]
    # A short explicit product switch can inherit a factual question, never an
    # order, payment, quantity or shipping action. Keep the basket untouched.
    if catalog and len(text.split()) <= 7 and re.match(r'^(?:а|і|та|а\s+у|а\s+в)\s+', text):
        named = catalog.explicit_products(text)
        topics = set(previous.get('topics', [])).intersection(TOPIC_HINTS)
        if (len(named) == 1 and len(topics) == 1 and not flags.intersection({'order','cancel','payment','delivery','complaint','availability'})
                and not requested_size(text) and not requested_color(text) and not requested_quantity(text)
                and not flags.intersection(INFORMATION)):
            return [raw + '\nПитання про ' + TOPIC_HINTS[next(iter(topics))] + '.']
    if (requested_size(text) and ('?' in text or re.match(r'^(?:а|для|у|в|на)\s+', text)) and
            re.fullmatch(r"(?:а\s+)?(?:для\s+|у\s+|в\s+|на\s+)?(?:розмір\w*\s+|размер\w*\s+)?[smlсмлхx]{1,3}[?!. ]*", text) and
            "measurements" in previous.get("topics", [])):
        return [raw + "\nЯкі заміри цього розміру?"]
    if (saved.get("awaiting") == "alternative" and len(saved.get("pending_alternatives", [])) == 1 and
            re.fullmatch(r"(?:так|да|ага|добре|окей)[, ]+(?:цей|це|його|її|той|цю|эту|этот|его|ее)(?:\s+(?:давайте|беру))?[.! ]*", text)):
        return ["так"]
    if (not flags.difference({"greeting", "availability", "sizing"}) and
            not requested_size(text) and not requested_color(text) and not requested_quantity(text)):
        topic = inferred_topic(text)
        if topic:
            return [raw + "\nПитання про " + TOPIC_HINTS[topic] + "."]
    return list(texts)


def grams(text):
    words = re.findall(r"[^\W\d_]+", canonical(text))
    return {word[i:i + 3] for word in words if len(word) >= 4 for i in range(len(word) - 2)}


@lru_cache(maxsize=1)
def question_model():
    path = Path(__file__).with_name("question_model.json")
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 128000:
            return {}
        data = json.loads(path.read_text())
        topics = data.get('topics') if isinstance(data, dict) and data.get('version') == 1 else None
        if not isinstance(topics, dict) or set(topics).difference(TOPIC_HINTS):
            return {}
        result = {}
        for topic, examples in topics.items():
            if not isinstance(examples, list) or len(examples) > 250:
                return {}
            if any(not isinstance(row, list) or len(row) > 300 or
                   any(not isinstance(token, str) or len(token) != 3 for token in row) for row in examples):
                return {}
            result[topic] = [frozenset(row) for row in examples]
        return result
    except (ValueError, OSError, TypeError):
        return {}


def inferred_topic(text):
    """Conservative lexical retrieval can suggest information, never a payment/action."""
    if len(text) > 250 or re.search(r"https?://|\d{5,}|скасу|отмен|не\s+треба|не\s+хочу", text):
        return None
    tokens = grams(text)
    if len(tokens) < 3:
        return None
    ranked = []
    for topic, examples in question_model().items():
        scores = [len(tokens.intersection(example)) / max(len(tokens), len(example)) for example in examples]
        ranked.append((max(scores, default=0), topic))
    ranked.sort(reverse=True)
    if len(ranked) < 2 or ranked[0][0] < 0.70 or ranked[0][0] - ranked[1][0] < 0.15:
        return None
    return ranked[0][1]


def shipping_intake(dialogue, texts, saved, advert, has_attachments):
    """One buyer turn may contain delivery data AND a separate side question.

    Consume validated fields before routing the question, then review the address
    once. A fact reply must never discard an adjacent message with the address.
    """
    from checkout import Checkout, asks_payment_method, shipping_details
    from customer_contact import contact_segments
    from conversation_policy import autonomous_reply
    if (not saved.get('invoice_issued') or saved.get('checkout_stage') != 'details' or saved.get('parcel', {}).get('ttn')):
        return None
    raw = '\n'.join(texts)
    if intents(raw).intersection({'cancel', 'returns', 'complaint'}):
        return None
    questions, fields = [], []
    for line in contact_segments(raw):
        flags = intents(line)
        if asks_payment_method(line) and flags.intersection(INFORMATION | {'shipment_status'}):
            questions.append(line)
        else:
            fields.append(line)
    if not questions or not (fields or has_attachments):
        return None
    intake_text = '\n'.join(fields)
    if 'order' in intents(intake_text):
        return None
    details = shipping_details(intake_text, saved.get('shipping_details', {}), saved.get('shipping_awaiting'))
    changed = details != saved.get('shipping_details', {})
    if not changed and not has_attachments:
        return None
    state = copy.deepcopy(saved)
    if changed:
        state.pop('customer_confirmation', None)
    intake = Checkout(dialogue.catalog, dialogue.inventory, dialogue.merchant, dialogue.now, dialogue.address_book).plan(
        intake_text, state, intents(intake_text), has_attachments)
    if intake is None:
        return None
    if intake['context'].get('receipt_submitted'):
        questions = [line for line in questions if not re.search(r'квитанц|\bчек\b', canonical(line))]
    if not questions:
        return intake
    state['shipping_details'] = details
    answer = dialogue._dispatch(questions, state, advert, False)
    if answer.on_send.get('shipment_notice_invoice') == state.get('invoice_id'):
        intake['on_send']['shipment_notice_invoice'] = state['invoice_id']
    body = autonomous_reply(answer, dialogue.catalog, state, '\n'.join(questions), advert) if answer.on_send.get('handoff_now') else answer.text
    if body and body != intake['text'] and len(body + intake['text']) + 2 <= 2000:
        intake['text'] = body + '\n\n' + intake['text']
        intake['guard'].update(answer.guard)
    return intake


def information_plan(dialogue, texts, saved, advert, has_attachments, allow_mixed=False):
    """Answer side questions without moving a buyer through checkout again."""
    raw = "\n".join(texts)
    text = canonical(raw)
    flags = intents(text)
    topics = flags.intersection(FACT_TOPICS | {"price", "measurements"})
    blocked = {"order", "cancel", "complaint", "returns"}
    if not allow_mixed:
        blocked.update({"payment", "delivery", "dispatch", "arrival"})
    if (has_attachments or not topics or flags.intersection(blocked) or
            requested_color(text) or requested_quantity(text)):
        return None
    if re.search(r'\b(?:бот|робот|людина|человек|власник|оператор|хозяин)\b', text):
        return None
    if saved.get('advice_pending') or (saved.get('awaiting') == 'fit' and len(text.split()) <= 3):
        return None
    from selection_guard import guard_selection
    if guard_selection(dialogue.catalog, dialogue.inventory, text, saved, advert, flags, has_attachments) is not None:
        return None
    # A requested size in a measurement question scopes measurements, not the basket.
    if requested_size(text) and "measurements" not in topics:
        return None
    if "sizing" in flags and "measurements" not in topics and topics != {"fit"}:
        return None
    if 'sizing' in flags:
        from customer_language import parameters
        if parameters(text, saved)[0]:
            return None
    explicit = dialogue.catalog.explicit_products(text)
    if len({row.get('product') for row in saved.get('cart', [])}) > 1 and len(explicit) != 1 and not saved.get('fact_subject'):
        return None
    product = (explicit[0] if len(explicit) == 1 else dialogue.catalog.products.get(saved.get('fact_subject')) or
               selected_product(dialogue.catalog, saved, advert))
    if not product or len(explicit) > 1:
        return None
    answers, missing = fact_answers(dialogue.catalog, product, text, flags)
    guard = {"fact_products": [product["key"]]}
    if "measurements" in topics:
        from customer_language import requested_sizes
        sizes = requested_sizes(text) or ([saved["size"]] if saved.get("size") and saved.get('product') == product['key'] else None)
        measured, incomplete = measurements_reply(product, sizes)
        if measured:
            answers.append("Ось заміри\n" + measured)
        if incomplete:
            missing.append("решту замірів цієї моделі")
    if "price" in topics:
        price = product.get("price_uah")
        if saved.get('invoice_issued'):
            from order_summary import order_items
            prices = {item.get('unit_price') for item in order_items(dialogue.catalog, saved) if item.get('product') == product['key']}
            if len(prices) == 1:
                price = prices.pop()
        if price is None:
            return None
        answers.append("Ціна — %s грн." % money(price))
    if missing:
        # Existing consultation provides richer recovery for missing facts.
        return None
    # "У них є фліс?" asks about insulation, not warehouse availability.
    stock_question = bool(re.search(r'наявн|налич|актуаль|прода[єе]те|залишил|остал|(?:^|[?!]\s*|\bа\s+)є\s+(?:ще\s+)?(?:штани|футболк)', text))
    if "availability" in flags and stock_question:
        try:
            rows = stock_rows(dialogue.inventory, product)
        except StockUnavailable:
            answers.append("Актуальну наявність зараз не вдалося перевірити. Спробую ще раз перед оформленням.")
        else:
            size = saved.get("size") if saved.get('product') == product['key'] else None
            color = saved.get("color") if saved.get('product') == product['key'] else None
            available = any(row["available"] >= saved.get("quantity", 1) and
                            (not size or row["size_code"] == size) and
                            (not color or row["color_code"] == color) for row in rows)
            answers.append("Так, обраний варіант є в наявності." if available else "Обраного варіанта зараз немає в наявності.")
            guard.update(product=product["key"], stock=stock_signature(rows))
    if not answers:
        return None
    context = dict(saved, fact_subject=product['key'])
    if not context.get('product'):
        context['product']=product['key']
    return {"text": "\n\n".join(dict.fromkeys(answers)), "context": context,
            "on_send": {}, "guard": guard, "notes": []}


def subject_guard(dialogue, raw, saved, has_attachments):
    """Clarify one genuinely ambiguous switch without changing the basket."""
    text=canonical(raw)
    flags=intents(text)
    subject=dialogue.catalog.products.get(saved.get('fact_subject'))
    chosen=dialogue.catalog.products.get(saved.get('product'))
    if (has_attachments or not subject or not chosen or subject['key']==chosen['key'] or
            dialogue.catalog.explicit_products(text) or
            flags.intersection(INFORMATION | {'cancel','returns','complaint','delivery','payment'}) or
            not (flags.intersection({'order'}) or requested_size(text) or requested_color(text) or requested_quantity(text))):
        return None
    context=dict(saved, pending_subject_choice={'text':raw[:300],'products':[chosen['key'],subject['key']]})
    return {'text':'Уточню, щоб правильно записати: зараз обираєте «'+subject['name']+'» чи «'+chosen['name']+'»?',
            'context':context,'on_send':{},'guard':{'fact_products':[subject['key'],chosen['key']]},'notes':[]}


def checkout_declaration(dialogue, raw, context, flags, has_attachments):
    """An unambiguous delivery choice also expresses the wish to place the order.

    Selecting a method is not a payment confirmation. The existing checkout
    validates every item, current stock and price before issuing any invoice.
    """
    from checkout import Checkout, asks_payment_method, method_choice, method_with_side_question
    text = canonical(raw)
    if (has_attachments or context.get('invoice_issued') or context.get('checkout_offered') or
            (asks_payment_method(text) and not method_with_side_question(raw)) or flags.intersection({'cancel', 'returns', 'complaint'}) or
            re.search(r'\b(?:без|не)\b', text)):
        return None
    method = method_with_side_question(raw) or method_choice(text)
    if method not in ('cod', 'full', 'olx') or re.fullmatch(r'\d[.! ]*', text):
        return None
    items = context.get('cart') or [context]
    if any(not all(row.get(key) for key in ('product', 'size', 'color')) for row in items):
        return None
    state = dict(context, checkout_offered=True, order_requested=True)
    return Checkout(dialogue.catalog, dialogue.inventory, dialogue.merchant, dialogue.now, dialogue.address_book).plan(raw, state, flags)


def finish(plan, saved, raw, interpreted=None):
    """Keep confirmed content while removing repeated sales prompts and filler."""
    original = canonical(raw)
    flags = intents(original)
    pure_information = bool(flags.intersection(INFORMATION)) and not flags.intersection({"order", "cancel", "complaint"})
    has_selection = bool(requested_size(original) or requested_color(original) or requested_quantity(original))
    if (pure_information and not has_selection and saved.get("awaiting") and
            not plan.on_send.get("awaiting") and not plan.on_send.get("invoice_issued") and
            plan.context.get("invoice_id") == saved.get("invoice_id")):
        plan.context["awaiting"] = saved["awaiting"]
        if "awaiting" in plan.on_send:
            plan.on_send["awaiting"] = saved["awaiting"]
    plan.text = plan.text.replace("Який колір вам подобається? Зараз є:", "Який колір обираєте? Є:")
    plan.text = plan.text.replace("Можна написати його назву українською, російською або англійською.", "")
    plan.text = re.sub(r"[ \t]+\n", "\n", plan.text).strip()
    if saved.get('advice_pending') and not plan.context.get('advice_pending') and len(plan.guard.get('fact_products',[])) == 1:
        plan.context['fact_subject']=plan.guard['fact_products'][0]
    if 'pending_subject_choice' not in plan.context and (has_selection or 'order' in flags) and not pure_information:
        plan.context.pop('fact_subject',None)
    elif plan.context.get('pending_subject_choice') and not plan.text.startswith('Уточню, щоб правильно записати:'):
        if has_selection or 'order' in flags or plan.context.get('product') != saved.get('product') or plan.guard.get('stock') or plan.guard.get('stocks'):
            plan.context.pop('pending_subject_choice',None)
            plan.context.pop('fact_subject',None)
    plan.on_send[MEMORY_KEY] = remember(saved, raw, plan.text, plan.context, plan.on_send, interpreted)
    return plan
