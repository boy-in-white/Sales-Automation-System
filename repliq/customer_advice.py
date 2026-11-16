"""Short factual answers that preserve the customer's selection and checkout state."""

import copy
import re

from customer_language import reports_payment
from order_summary import order_items
from shop_facts import money, shop_day, shipping_reply

FACT_TOPICS = {"material", "waist", "lining", "season", "pilling", "quality", "description", "discount", "fit", "rise", "size_fit", "temperature", "print", "transparency"}
SEASONS = {"spring": (r"весн|весенн|навесн", "на весну"),
           "summer": (r"літо|літа|літку|літні|влітку|лето|летом|летни", "на літо"),
           "autumn": (r"осін|осен|наосінь", "на осінь"),
           "winter": (r"зим|взимку", "на зиму")}


def fact_answers(catalog, product, text, flags):
    """Return known answers and names of missing facts; never borrow another SKU's facts."""
    facts = product.get("facts", {}) if product else {}
    answers, missing = [], []
    specific = flags.intersection(FACT_TOPICS - {"description", "discount"})
    waist_question = bool(re.search(r"(?:обхват|напівобхват|півобхват|полуобхват|по\s+колу|ширина).{0,20}(?:пояс|талі)|(?:пояс|талі).{0,25}(?:обхват|ширин|без\s+розтяг|без\s+растяг)|як.{0,15}(?:мірял|вимір|замір)", text))
    if "waist" in specific and waist_question and product and product.get("waist_measurement_kind") == "flat_width":
        state = " без розтягування" if product.get("waist_measurement_state") == "relaxed" else ""
        answers.append("Заміри пояса вказані як ширина штанів, розкладених на поверхні" + state + ".")
    for topic in ("material", "waist", "lining", "pilling", "quality", "fit", "rise", "size_fit", "print", "transparency"):
        if topic not in specific or topic == "quality" and "pilling" in specific:
            continue
        if topic == "lining" and "season" in specific:
            continue
        if topic == "waist" and waist_question:
            continue
        if topic == "fit" and "rise" in specific:
            continue
        if topic == "waist" and re.search(r"тканин|ткань", text) and not re.search(r"пояс|гумк|рез[іи]нк|шнур", text):
            missing.append("еластичність самої тканини")
            continue
        if facts.get(topic):
            answers.append(facts[topic])
        elif topic == 'quality' and any(facts.get(key) for key in ('material', 'fit', 'transparency')):
            # Explain verified properties instead of inventing durability claims.
            answers.extend(facts[key] for key in ('material', 'fit', 'transparency') if facts.get(key))
        else:
            missing.append({"material": "точний склад тканини", "waist": "еластичність пояса й наявність шнурка",
                            "lining": "утеплення цієї моделі", "pilling": "стійкість цієї тканини до катишків",
                            "quality": "характеристики цієї моделі", "fit": "крій і посадку", "rise": "висоту посадки",
                            "size_fit": "відповідність биркового розміру", "print": "спосіб нанесення й стійкість декору",
                            "transparency": "чи просвічується ця модель"}[topic])
    if "fit" in specific and "rise" not in specific and re.search(r"посадк", text) and facts.get("rise"):
        answers.append(facts["rise"])
    if "temperature" in specific:
        limits = facts.get("comfort_temperature_c")
        if limits:
            answers.append("Орієнтовно комфортно носити від %s до %s °C." % (format(limits[0], "+g").replace("-", "−"), format(limits[1], "+g")))
        else:
            missing.append("комфортний температурний діапазон цієї моделі")
    if "season" in specific and "temperature" not in specific:
        available = facts.get("seasons", [])
        asked = [key for key, (pattern, _) in SEASONS.items() if re.search(pattern, text)]
        if not available:
            missing.append("сезонність цієї моделі")
        elif not asked:
            seasons = [SEASONS[key][1] for key in SEASONS if key in available]
            answers.append("Підійдуть " + ", ".join(seasons[:-1]) + (" і " if len(seasons) > 1 else "") + seasons[-1] + ". " + facts.get("lining", ""))
        elif all(key in available for key in asked):
            names = [SEASONS[key][1] for key in asked]
            answers.append("Так, " + " і ".join(names) + " підійдуть.")
        elif asked == ["summer"] and facts.get("lining"):
            answers.append("Для літа можуть бути затеплими: усередині є утеплення.")
        else:
            supported = [SEASONS[key][1] for key in asked if key in available]
            if supported:
                answers.append("На вказані сезони підходять частково: " + " і ".join(supported) + ".")
            missing.append("чи підійде ця модель " + " і ".join(SEASONS[key][1] for key in asked if key not in available))
    if "description" in flags and not specific:
        if product and product.get("description"):
            answers.append(product["description"])
        else:
            missing.append("склад і характеристики тканини")
    if "discount" in flags:
        if catalog.shop.get("discount"):
            answers.append(catalog.shop["discount"])
        else:
            missing.append("можливість знижки")
    return list(dict.fromkeys(answers)), list(dict.fromkeys(missing))


def dispatch_plan(catalog, saved, text, now=None):
    context = copy.deepcopy(saved)
    parcel = saved.get("parcel", {})
    if parcel.get("ledgerly_status") == "shipped":
        body = "Посилку вже відправлено."
        if parcel.get("ttn") and re.fullmatch(r"\d{14}", str(parcel["ttn"])):
            body += " Ваша ТТН: " + parcel["ttn"] + "."
        return {"text": body, "context": context, "on_send": {}, "guard": {}, "notes": []}
    day = str(shop_day(catalog.shop, now))
    repeated = context.get("dispatch_day") == day and context.get("dispatch_queries", 0) > 0
    context.update(dispatch_day=day, dispatch_queries=min(20, context.get("dispatch_queries", 0) + 1) if repeated else 1)
    body = shipping_reply(catalog.shop, now)
    if repeated:
        body = ("Так, відправка запланована сьогодні протягом дня." if shop_day(catalog.shop, now).weekday() < 5 else
                "За можливості відправимо сьогодні. Якщо не встигнемо — з понеділка.")
    if saved.get("invoice_issued") and not saved.get("payment_verified"):
        details = saved.get("shipping_details", {})
        labels = {"name": "ПІБ", "phone": "телефон", "place": "населений пункт і область", "branch": "номер відділення"}
        missing = [label for key, label in labels.items() if not details.get(key)]
        if missing:
            body += " Для оформлення ще потрібні: " + ", ".join(missing) + "."
    if not saved.get("greeted"):
        body = "Добрий день! " + body
    return {"text": body, "context": context, "on_send": {"greeted": True},
            "guard": {"shipping_day": day}, "notes": []}


def advice_plan(catalog, text, saved, advert_id, flags, now=None, has_attachments=False):
    """Handle FAQ detours before checkout parsers can mistake them for name/address."""
    if saved.get('parcel', {}).get('status') == 'cancelled' and flags.intersection({'dispatch','shipment_status'}):
        return {'text': 'Це оформлення скасоване. Якщо бажаєте, допоможу оформити нове замовлення.',
                'context': copy.deepcopy(saved), 'on_send': {}, 'guard': {}, 'notes': []}
    if (saved.get("awaiting") in ("fit", "garment_measurements", "usual_size") and
            flags.intersection(FACT_TOPICS) == {"fit"} and not re.search(r"\?|крій|посадк|як\b|како", text)):
        return None
    tracking_question = "shipment_status" in flags and not re.search(r"скарг|жалоб|обман|повреж|пошкод|не\s+прийш|не\s+приш|оператор", text)
    if (has_attachments or reports_payment(text) or flags.intersection({"cancel", "returns"}) or "complaint" in flags and not tracking_question or
            re.search(r"(?:піб|фіо|фио|телефон|відділення|отделение|місто|город)\s*[:—]", text)):
        return None
    context = copy.deepcopy(saved)
    if "shipment_status" in flags:
        from order_summary import snapshot, confirmation_key, invoice_key
        order = snapshot(catalog, saved)
        parcel = order.get("parcel", {})
        ttn = str(parcel.get("ttn", ""))
        if (parcel.get("status") == "created" and re.fullmatch(r"\d{14}", ttn) and
                saved.get("payment_verified") and saved.get("verified_invoice_key") == invoice_key(order) and
                saved.get("customer_confirmation") == confirmation_key(order)):
            body = "Посилку вже відправлено. " if parcel.get("ledgerly_status") == "shipped" else "Накладну підготовлено. "
            body += "Ось ваша ТТН: " + ttn + "."
            if parcel.get("ledgerly_status") != "shipped":
                body += " " + shipping_reply(catalog.shop, now)
            return {"text": body, "context": context, "on_send": {},
                    "guard": {"shipping_day": str(shop_day(catalog.shop, now))}, "notes": []}
        return {"text": "Перевіримо фактичний статус відправлення й надішлемо вам актуальну інформацію. Підключу власника.",
                "context": context, "on_send": {"greeted": True, "handoff_now": True}, "guard": {},
                "notes": ["Потрібен фактичний статус/ТТН. Планова дата не є доказом відправлення."]}
    if (saved.get("checkout_offered") and not saved.get("invoice_issued") and
            re.search(r"налож|післяплат|послеплат|накладен|олх|olx|повн\w*\s+(?:оплат|передоплат)|полн\w*\s+(?:оплат|предоплат)", text)):
        return None
    followup = context.get("dispatch_queries") and re.fullmatch(r"(?:(?:ну+|а)\s+)?(?:коли|когда|сьогодні|сегодня|точно|ще\s+довго|еще\s+долго)[?! .]*", text)
    if ("dispatch" in flags or followup) and not flags.intersection({"order", "price", "availability", "material", "waist", "lining", "season", "pilling", "quality", "measurements"}):
        return dispatch_plan(catalog, saved, text, now)
    pending = context.get("advice_pending")
    explicit = catalog.explicit_products(text)
    if not flags.intersection(FACT_TOPICS):
        if not pending or len(explicit) != 1 or flags.intersection({"order", "availability", "sizing", "price", "dispatch"}):
            return None
        flags, text = set(pending["flags"]), pending["text"]
    # An actual new selection goes through the regular stock/cart handler instead.
    if flags.intersection({"order", "measurements", "photos", "payment", "arrival"}) or "delivery" in flags and "dispatch" not in flags:
        return None
    if re.search(r"наяв|налич|актуаль|є\s+(?:[smlсмлх](?!\w)|чорн|черн|біл|бел|сір|сер)|(?:[smlсмл])\s+є\b", text):
        return None
    if re.search(r"розмір|размер|зріст|\bріст\b|\bрост\b|\bваг[ауи]\b|\bвес\b", text) and not flags.intersection({"season", "size_fit"}):
        return None
    products = explicit
    if not products:
        keys = {item["product"] for item in order_items(catalog, saved)}
        if not keys:
            key = saved.get("product") or catalog.adverts.get(str(advert_id))
            keys = {key} if key else set()
        products = [catalog.products[key] for key in keys if key in catalog.products]
        if len(products) > 1 and flags.intersection(FACT_TOPICS - {"discount"}):
            context["advice_pending"] = {"text": text[:1500], "flags": sorted(flags)}
            return {"text": "Про яку річ уточнити — " + " чи ".join(p["name"] for p in products[:5]) + "?",
                    "context": context, "on_send": {}, "guard": {}, "notes": []}
    if not products and flags.intersection(FACT_TOPICS - {"discount"}):
        return None
    if len(products) > 5:
        return None
    if not flags.intersection(FACT_TOPICS - {"discount"}) and "price" not in flags:
        products = []
    parts, unknown = [], []
    for product in products or [None]:
        answers, missing = fact_answers(catalog, product, text, flags)
        if "price" in flags:
            if product and product.get("price_uah") is not None:
                prices = [item.get("unit_price") for item in order_items(catalog, context) if product["key"] == item["product"]]
                price = prices[0] if context.get("invoice_issued") and len(prices) == 1 else product["price_uah"]
                if price is not None:
                    answers.insert(0, "Ціна — %s грн." % money(price))
                else:
                    missing.append("погоджену ціну")
            else:
                missing.append("актуальну ціну")
        if missing:
            answers.append("Уточню для вас " + ", ".join(missing) + ".")
            unknown.extend(missing)
        if answers:
            parts.append((product["name"] + ":\n" if len(products) > 1 else "") + " ".join(answers))
    if not parts:
        return None
    context.pop("advice_pending", None)
    guard = {"fact_products": [p["key"] for p in products]} if products else {}
    if "dispatch" in flags:
        shipping = dispatch_plan(catalog, dict(context, greeted=True), text, now)
        context = shipping["context"]
        parts.append(shipping["text"])
        guard.update(shipping["guard"])
    if not context.get("product") and len(products) == 1:
        context["product"] = products[0]["key"]
    greeting = "Добрий день! " if not saved.get("greeted") else ""
    body = greeting + "\n\n".join(parts)
    if len(body) > 2000:
        return None
    sent = {"greeted": True}
    if unknown:
        sent["handoff_now"] = True
    return {"text": body, "context": context, "on_send": sent, "guard": guard, "notes": unknown}


def append_fact_answers(plan, catalog, text, flags, advert_id):
    """Answer a factual clause alongside an actual selection/payment action."""
    if not flags.intersection(FACT_TOPICS) or flags.intersection({"cancel", "complaint"}) or reports_payment(text):
        return plan
    explicit = catalog.explicit_products(text)
    keys = {item["product"] for item in order_items(catalog, dict(plan["context"], **plan["on_send"]))}
    if not keys:
        key = plan["context"].get("product") or catalog.adverts.get(str(advert_id))
        keys = {key} if key else set()
    products = explicit or [catalog.products[key] for key in keys if key in catalog.products]
    if len(products) > 1 and not explicit and flags.intersection(FACT_TOPICS - {"discount"}):
        plan["context"]["advice_pending"] = {"text": text[:1500], "flags": sorted(flags.intersection(FACT_TOPICS))}
        extra = "Про яку річ уточнити характеристики — " + " чи ".join(p["name"] for p in products[:5]) + "?"
    else:
        parts, unknown = [], []
        if not flags.intersection(FACT_TOPICS - {"discount"}):
            products = []
        for product in products[:5] or [None]:
            answers, missing = fact_answers(catalog, product, text, flags)
            if missing:
                answers.append("Уточню для вас " + ", ".join(missing) + ".")
                unknown.extend(missing)
            if answers:
                parts.append((product["name"] + ": " if product and len(products) > 1 else "") + " ".join(answers))
        extra = "\n".join(parts)
        if unknown:
            plan["on_send"].update(handoff_now=True)
            plan["notes"].extend(unknown)
    plan["guard"]["fact_products"] = [p["key"] for p in products]
    combined = extra + "\n\n" + plan["text"] if extra else plan["text"]
    if len(combined) > 2000:
        combined = "Уточнимо характеристики й оформлення цього замовлення особисто, щоб нічого не переплутати."
        plan["on_send"].update(handoff_now=True, invoice_issued=False, checkout_stage="owner")
    plan["text"] = combined
    return plan
