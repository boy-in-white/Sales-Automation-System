"""Private height rules; recommendations are made only when the buyer requests help."""

import re

from customer_language import parameters
from customer_phrasing import canonical


def preferred_available_size(rows, candidates, color=None, quantity=1, preferred=None):
    """Rank only already suitable sizes; never invent or relabel stock."""
    counts={size:sum(max(0,row['available']) for row in rows
                    if row['size_code']==size and (not color or row['color_code']==color)
                    and row['available']>=quantity) for size in candidates}
    return max(candidates,key=lambda size:(counts[size],size==preferred,-candidates.index(size)))


def asks_size_advice(text, awaiting=None):
    text = canonical(text)
    explicit = bool(re.search(
        r"(?:який|якого|какой|какого).{0,20}(?:розмір|размер).{0,35}(?:мені|мне|підій|подойд|порад|рекоменд)|"
        r"(?:розмір|размер).{0,25}(?:підій|подойд|порад|рекоменд)|"
        r"(?:порад|порекоменду|рекоменд|посовет|підбер|підб[іо]р|подбер|поможи|допомож|помог).{0,35}(?:розмір|размер)|"
        r"підкаж.{0,12}з\s+розмір|"
        r"(?<!\w)(?:[smlсмл]|xl|хл)(?!\w).{0,25}(?:підій|подойд)|"
        r"(?:підій|подойд|порад|рекоменд|посовет).{0,25}(?<!\w)(?:[smlсмл]|xl|хл)(?!\w)|"
        r"(?:зріст|зрост|ріст|рост).{0,30}(?:який|какой).{0,15}(?:розмір|размер)|"
        r"(?:який|какой).{0,15}(?:розмір|размер).{0,30}(?:зріст|зрост|ріст|рост)", text))
    fit_with_parameters = bool(re.search(r"зріст|ріст|рост|\b\d{3}\s*[/ ]\s*\d{2,3}\b", text) and
                               re.search(r"підій|подойд|будуть|будет|кращ|лучш|який|какой|що\s+взяти|что\s+взять", text))
    choosing_sizes = awaiting in ("size", "cart_size", "parameters") and bool(re.search(r"(?:думаю|кращ|лучш).{0,30}(?<!\w)[smlсмл](?!\w).{0,12}(?:чи|или|або)", text))
    bare_help = bool(re.search(r'(?:який|какой)\s+(?:розмір|размер)[?!. ]*$', text) and
                     not re.search(r'наявн|налич|\bє\b|\bесть\b|доступн|на\s+фото|модел', text))
    supplied_parameters = bool((re.search(r'(?:зріст|ріст|рост).{0,22}\d', text) or
                                re.fullmatch(r'(?:1[2-9]\d|2[0-2]\d|230|[12][.,]\d{1,2})\s*[/; ]\s*\d{2,3}[.! ]*', text)) and
                               not re.search(r'на\s+фото|модел|людині|человек|довжин|длин|ширин|обхват|телефон|\d{7,}', text))
    range_advice=bool(re.search(r'розмір|размер',text) and re.search(r'\d{3}\s*[-–—−]\s*\d{3}',text)
                      and not re.search(r'довжин|длин|ширин|замір|замер|обхват',text))
    return range_advice or explicit or fit_with_parameters or choosing_sizes or bare_help or supplied_parameters or awaiting == "consultation_help" and bool(re.search(r"розмір|размер|р[іо]ст|зріст", text)) or awaiting in ("size", "cart_size") and bool(re.search(r"порад|рекоменд|посовет|який.{0,15}кращ|какой.{0,15}лучш", text))


def height_advice(product, text, context):
    if product.get("sizing_basis") != "height":
        return None
    values, errors = parameters(text, context, weight_for_fit=False)
    if re.search(r"для\s+(?:інш|друг|брат|сестр|чоловік|дружин|дитин|реб[её]н|сина|сына|доньк|дочер|мам|тат|отц|матер)|не\s+для\s+(?:мене|меня)", text):
        for key in ("height", "height_range", "weight", "weight_followup_asked", "weight_declined"):
            context.pop(key, None)
    if 'height' in values and 'height_range' not in values:
        context.pop('height_range',None)
    if values.get('height') and values['height']!=context.get('height') and not context.get('weight') and not values.get('weight'):
        context.pop('weight_followup_asked',None)
    context.update(values)
    context["size_advice_requested"] = True
    if errors:
        return {"question": errors[0], "awaiting": "parameters"}
    if re.search(r"(?:не\s+(?:знаю|пам.?ятаю|помню|хочу)|без).{0,25}(?:ваг|вес)|(?:ваг|вес).{0,25}не\s+(?:знаю|пам.?ятаю|помню|хочу)", text) or (
            context.get("awaiting") == "weight" and re.search(r"не\s+(?:знаю|хочу|пам.?ятаю|помню)", text)):
        context["weight_declined"] = True
    if not context.get("height"):
        if context.get("weight") or context.get("weight_declined"):
            return {"question": "Підкажіть ще, будь ласка, ваш зріст.", "awaiting": "height"}
        return {"question": "Допоможу підібрати. Напишіть, будь ласка, ваш зріст і вагу.", "awaiting": "parameters"}
    if not context.get("weight") and not context.get("weight_declined") and not context.get("weight_followup_asked"):
        context["weight_followup_asked"] = True
        return {"question": "Дякую! Підкажіть ще, будь ласка, вашу вагу.", "awaiting": "weight"}
    guide = product.get("height_guidance", {})
    interval=context.get('height_range')
    if interval:
        matched=[]
        # Endpoints alone can hide an uncovered gap. Check each centimetre and
        # both fractional endpoints against the configured guide.
        points=[interval[0],interval[1]]+list(range(int(interval[0])+1,int(interval[1])+1))
        for height in points:
            options=[(limits[0],size) for size,limits in guide.items()
                     if limits[0]<=height<=limits[1] and size not in product.get('excluded_sizes',[])]
            if not options:
                return {'question':'Підкажіть точніший зріст, будь ласка: частина цього діапазону виходить за орієнтири моделі. Можемо також порівняти заміри.', 'awaiting':'height'}
            matched.append(max(options)[1])
        choices=list(dict.fromkeys(matched))
        if len(choices)>1:
            context['size_advice_requested']=False
            return {'candidates':choices, 'size':choices[-1]}
    candidates = [(limits[0], size) for size, limits in guide.items()
                  if limits[0] <= context["height"] <= limits[1] and size not in product.get("excluded_sizes", [])]
    if not candidates:
        return {"question": "Для вас краще уточнити посадку цієї моделі особисто, щоб правильно підібрати розмір. Передам запит власнику.",
                "awaiting": None, "human": True}
    # At a shared boundary use the interval that starts at this height.
    chosen = max(candidates)[1]
    context["size_advice_requested"] = False
    result={"size": chosen}
    if len(candidates)>1:
        result['candidates']=[size for _,size in sorted(candidates)]
    return result
