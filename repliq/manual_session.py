"""Carry explicit buyer choices back from a manual session, never payment claims."""

import copy
import re

from customer_phrasing import canonical
from customer_language import intents, parameters, requested_size, requested_color, requested_quantity


def selection_after_manual(catalog, saved, history, since):
    context = copy.deepcopy(saved)
    if not since or saved.get('invoice_issued') or saved.get('cart'):
        return context  # Frozen orders and multi-item baskets need their normal guarded path.
    for message in history:
        if message.get('type') != 'received' or message.get('_time', 0) < since:
            continue
        text = canonical(message.get('text') or '')
        flags = intents(text)
        if ('?' in text or flags.intersection({'availability','measurements','cancel','complaint','returns','payment'}) or
                re.search(r'\b(?:не|чи|або|или)\b', text)):
            continue
        body, errors = parameters(text, context, weight_for_fit=False)
        if body and not errors:
            context.update(body)
        size, color, quantity = requested_size(text), requested_color(text), requested_quantity(text)
        named = catalog.explicit_products(text)
        if len(named) > 1 or not (size or color or quantity):
            continue
        explicit_choice = ('order' in flags or len(named) == 1 or
                           bool(re.fullmatch(r'(?:[smlсмл]|xl|хл|xxl|ххл|чорн\w*|черн\w*|біл\w*|бел\w*|сір\w*|сер\w*|black|white|gr[ae]y)(?:[\s,.!]+(?:[smlсмл]|xl|хл|чорн\w*|черн\w*|біл\w*|бел\w*|сір\w*|сер\w*|black|white|gr[ae]y))*[.! ]*', text)))
        product = named[0] if len(named) == 1 else catalog.products.get(context.get('product') or context.get('_ad_product'))
        if not explicit_choice or not product or size in product.get('excluded_sizes', []):
            continue
        if context.get('product') != product['key']:
            for key in ('size','color','quantity'):
                context.pop(key, None)
        context['product'] = product['key']
        if size:
            context['size'] = size
        if color:
            context['color'] = color
        if quantity is not None and 1 <= quantity <= 20:
            context['quantity'] = quantity
        for key in ('checkout_offered','checkout_stage','pending_alternatives','size_advice_requested','advice_pending','awaiting'):
            context.pop(key, None)
        context['topic'] = 'availability'
    return context
