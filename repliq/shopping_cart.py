"""A small per-conversation basket. Ambiguous attributes are clarified, never guessed."""

import copy
import re
from collections import Counter

from catalog import COLORS, SIZES, normalize
from customer_language import requested_size, requested_sizes, requested_color, requested_colors, requested_quantity
from size_advice import asks_size_advice, height_advice
from inventory import StockUnavailable, stock_signature, stock_rows
from shop_facts import alternatives, choice_text, measurements_reply, stock_groups, money, total_price

SEPARATOR = re.compile(r"[,;\n]+|\s+(?:і|й|та|и|плюс|а ще|також|еще|ещё)\s+")
YES = re.compile(r"(?:так|да|ага|ок|окей|добре|давайте|підходить|подходит|беру|замовляю)[.!😊 ]*")


def mentions(catalog, text):
    found = []
    for product in catalog.products.values():
        for alias in [product["name"]] + product["aliases"]:
            for match in re.finditer(r"(?<!\w)" + re.escape(normalize(alias)) + r"(?!\w)", text):
                found.append((match.start(), match.end(), product["key"]))
    for kind, pattern in (("pants", r"\b(?:штан\w*|брюк\w*|pants)\b"), ("tee", r"\b(?:футбол\w*|t-shirt|tee)\b")):
        choices = [p for p in catalog.products.values() if p["kind"] == kind]
        if len(choices) == 1:
            found.extend((m.start(), m.end(), choices[0]["key"]) for m in re.finditer(pattern, text))
    selected = []
    for item in sorted(set(found), key=lambda x: (x[0], -(x[1] - x[0]))):
        if selected and item[0] < selected[-1][1]:
            continue
        if selected and item[2] == selected[-1][2] and not text[selected[-1][1]:item[0]].strip():
            selected[-1] = (selected[-1][0], item[1], item[2])
        else:
            selected.append(item)
    return selected


def parse_items(catalog, text, default_product=None):
    refs = mentions(catalog, text)
    if not refs:
        refs = [(0, 0, default_product)] if default_product else []
    starts = [0]
    for previous, current in zip(refs, refs[1:]):
        separators = list(SEPARATOR.finditer(text, previous[1], current[0]))
        starts.append(separators[-1].end() if separators else current[0])
    items, ambiguous = [], False
    for index, ref in enumerate(refs):
        segment = text[starts[index]:starts[index + 1] if index + 1 < len(starts) else len(text)]
        pieces, start = [], 0
        for separator in SEPARATOR.finditer(segment):
            left, right = segment[start:separator.start()], segment[separator.end():]
            if (requested_size(left) or requested_color(left)) and (requested_size(right) or requested_color(right)):
                pieces.append(left)
                start = separator.end()
        pieces.append(segment[start:])
        for piece in pieces:
            sizes = requested_sizes(piece)
            colors = set(requested_colors(piece))
            ambiguous |= len(sizes) > 1 or len(colors) > 1
            quantity = requested_quantity(piece)
            items.append({"product": ref[2], "size": sizes[0] if len(sizes) == 1 else None,
                          "color": next(iter(colors)) if len(colors) == 1 else None,
                          "quantity": quantity if quantity is not None else 1})
    return items, ambiguous


class ShoppingCart:
    def __init__(self, catalog, inventory):
        self.catalog, self.inventory = catalog, inventory

    def plan(self, text, saved, advert_id, flags):
        default = saved.get("product") or self.catalog.adverts.get(str(advert_id))
        parsed, ambiguous = parse_items(self.catalog, text, default)
        if not saved.get("cart") and len(parsed) < 2:
            return None
        # General questions are answered by the common fact handler without changing the basket.
        if saved.get("cart") and flags and flags <= {"dispatch", "delivery", "payment", "returns", "greeting", "thanks", "arrival"}:
            return None
        context, guards, notes, sent = copy.deepcopy(saved), {}, [], {"greeted": True}
        cart = copy.deepcopy(context.get("cart", []))
        greeting = "Добрий день! 😊\n\n" if "greeting" in flags and not saved.get("greeted") else ""

        def reply(body, awaiting="cart", human=False, checkout=False):
            if len(greeting + body) > 2000:
                body, human, checkout = "У вас велике замовлення. Допоможемо узгодити всі деталі особисто, щоб нічого не пропустити.", True, False
            context["cart"] = cart
            context["awaiting"] = awaiting
            if cart:
                context["product"] = cart[0]["product"]
            sent["awaiting"] = awaiting
            if human:
                sent["handoff_now"] = True
            if checkout:
                sent["checkout_offered"] = True
            response = greeting + body
            if response.count("😊") > 1:
                first = response.index("😊") + 1
                response = response[:first] + response[first:].replace("😊", "").rstrip()
            return {"text": response, "context": context, "on_send": sent,
                    "guard": {"stocks": guards} if guards else {}, "notes": notes}

        if any(item.get("product") not in self.catalog.products for item in cart):
            return reply("Каталог змінився. Уточнимо вибрані речі, щоб оформити замовлення правильно.", human=True)
        if ambiguous and not (saved.get("cart") and asks_size_advice(text, saved.get("awaiting"))):
            return reply("Допоможу з кількома речами 😊 Напишіть кожну окремо: товар, розмір, колір і кількість. Наприклад: «штани M чорні — 1 шт.; футболка L біла — 2 шт.». Не хочу переплутати ваш вибір.", "cart_details")
        if "complaint" in flags:
            return reply("Уточню деталі особисто, щоб допомогти вам із замовленням.", human=True)
        if "cancel" in flags:
            cart.clear()
            context.pop("cart_order_requested", None)
            context.pop("cart_pending", None)
            return reply("Добре, замовлення скасовано. Якщо захочете повернутися до вибору — пишіть 😊", None)
        removing = bool(re.search(r"прибери|убери|видал|без\s+(?:футбол|штан)|не\s+хочу\s+(?:футбол|штан)", text))
        adding = bool(re.search(r"додай|добав|\bплюс\b", text) or
                      re.search(r"\b(?:ще|еще|додатково)\b", text) and flags.intersection({'order'}))
        refs = mentions(self.catalog, text)
        focus = context.get("cart_focus", 0)
        pending_edit = context.pop("cart_edit", None)
        line_match = re.search(r"(?:позиці[яюї]|позиц[ияю]|номер|№)\s*(\d{1,2})", text)
        if line_match:
            focus = int(line_match[1]) - 1
            if not 0 <= focus < len(cart):
                return reply("Такої позиції ще немає. Підкажіть назву речі, яку хочете змінити 😊", "cart_details")
        advice_message = ""
        wants_advice = asks_size_advice(text, saved.get("awaiting"))
        if requested_size(text) and "order" in flags and not wants_advice:
            context["size_advice_requested"] = False
            context.pop("cart_advice_choose", None)
        if cart and (wants_advice or context.get("size_advice_requested") or context.get("cart_advice_choose")):
            targets = [i for i, item in enumerate(cart) if any(ref[2] == item["product"] for ref in refs)]
            if not targets and context.get("size_advice_requested") and context.get("cart_advice_focus") is not None:
                targets = [context["cart_advice_focus"]]
            if not targets and len(cart) == 1:
                targets = [0]
            if line_match:
                targets = [focus]
            if len(targets) != 1 or not 0 <= targets[0] < len(cart):
                context["cart_advice_choose"] = True
                return reply("Для якої речі підібрати розмір — " + " чи ".join(dict.fromkeys(self.catalog.products[i["product"]]["name"] for i in cart)) + "?", "cart_advice_product")
            focus = targets[0]
            context["cart_advice_focus"] = focus
            context.pop("cart_advice_choose", None)
            product = self.catalog.products[cart[focus]["product"]]
            if wants_advice and not context.get("size_advice_requested"):
                context.pop("weight_followup_asked", None)
            step = height_advice(product, text, context)
            if step is None:
                return reply("Для цієї речі уточнимо посадку особисто, щоб підібрати розмір правильно.", human=True)
            if step.get("question"):
                return reply(step["question"], step.get("awaiting"), human=step.get("human", False))
            cart[focus]["size"] = step["size"]
            advice_message = "%s: вам підійде %s." % (product["name"], step["size"])
            context.pop("cart_advice_focus", None)
            context.pop("cart_pending", None)
        pending = context.pop("cart_pending", None)
        if advice_message:
            parsed = []
        elif pending and YES.fullmatch(text):
            if len(pending["choices"]) != 1:
                context["cart_pending"] = pending
                return reply("Який саме варіант обираєте? Напишіть розмір і колір, будь ласка 😊", "cart_choice")
            focus = pending["line"]
            if 0 <= focus < len(cart):
                cart[focus].update(pending["choices"][0])
            parsed = []
        elif removing:
            targets = [i for i, item in enumerate(cart) if any(r[2] == item["product"] for r in refs)
                       and (not requested_size(text) or item["size"] == requested_size(text))
                       and (not requested_color(text) or item["color"] == requested_color(text))] if refs else [focus]
            if len(targets) != 1 or not 0 <= targets[0] < len(cart):
                return reply("Яку саме позицію прибрати? Напишіть «позиція 1» або назву з розміром і кольором.", "cart_details")
            cart.pop(targets[0])
            parsed = []
        elif cart and not refs:
            if pending_edit and line_match:
                cart[focus].update(pending_edit)
                parsed = []
            elif not (requested_size(text) or requested_color(text) or requested_quantity(text)):
                parsed = []
                if YES.fullmatch(text) and context.get("awaiting") == "cart_order":
                    context["cart_order_requested"] = True
                elif not YES.fullmatch(text) and not flags.intersection({"availability", "order", "price", "measurements"}):
                    return reply("Підкажіть, що хочете змінити чи уточнити в замовленні? Допоможу 😊", human=True)
            elif 0 <= focus < len(cart):
                updates = {"size": requested_size(text), "color": requested_color(text), "quantity": requested_quantity(text)}
                if len(cart) > 1 and not line_match and saved.get("awaiting") not in ("cart_size", "cart_color", "cart_choice", "cart_quantity"):
                    sent["cart_edit"] = {key: value for key, value in updates.items() if value is not None}
                    return reply("Для якої речі змінити вибір? Напишіть назву або номер позиції 😊", "cart_target")
                cart[focus].update({key: value for key, value in updates.items() if value is not None})
                parsed = []
        for item in parsed:
            if pending_edit:
                for key, value in pending_edit.items():
                    if item.get(key) is None or key == "quantity" and requested_quantity(text) is None:
                        item[key] = value
            if not item["product"] or item["product"] not in self.catalog.products:
                return reply("Які саме речі обираємо? Напишіть назви, будь ласка 😊", "cart_details")
            candidates = [i for i, old in enumerate(cart) if old["product"] == item["product"]]
            exact = [i for i in candidates if cart[i].get("size") == item["size"] and cart[i].get("color") == item["color"]]
            if not cart or adding or not candidates or len(parsed) > 1 and not saved.get("cart"):
                cart.append(item)
            elif len(exact) == 1:
                cart[exact[0]].update(item)
            elif len(candidates) == 1:
                target = cart[candidates[0]]
                target.update({k: v for k, v in item.items() if v is not None and k != "quantity"})
                if requested_quantity(text) is not None or pending_edit and "quantity" in pending_edit:
                    target["quantity"] = item["quantity"]
            else:
                return reply("Для цього товару вже є кілька позицій. Уточніть номер позиції, розмір і колір, щоб я змінив саме потрібну.", "cart_details")
        if any(not 1 <= item["quantity"] <= 20 for item in cart):
            return reply("Уточнимо потрібну кількість, щоб правильно зібрати замовлення.", human=True)
        if not cart:
            context.pop("cart", None)
            return reply("У списку поки немає речей. Що бажаєте замовити? 😊", "product")
        merged = []
        for item in cart:
            same = next((old for old in merged if item["size"] and item["color"] and
                         all(old[k] == item[k] for k in ("product", "size", "color"))), None)
            if same:
                same["quantity"] += item["quantity"]
            else:
                merged.append(item)
        cart = merged
        if len(cart) > 10 or any(not 1 <= item["quantity"] <= 20 for item in cart):
            notes.append("Велике або нестандартне замовлення: узгодити кількість особисто.")
            cart[:] = cart[:10]
            return reply("Допоможу оформити таке замовлення особисто, щоб нічого не пропустити 😊", human=True)
        if "order" in flags:
            context["cart_order_requested"] = True
        if "measurements" in flags and not flags.intersection({"availability", "order"}):
            sections, missing = [], False
            for item in cart:
                product = self.catalog.products[item["product"]]
                body, unknown = measurements_reply(product, [item["size"]] if item["size"] else None)
                sections.append(product["name"] + ":\n" + body)
                missing |= unknown
            return reply("Ось заміри в сантиметрах 😊\n\n" + "\n\n".join(sections), human=missing)
        stocks = {}
        try:
            for item in cart:
                product = self.catalog.products.get(item["product"])
                if not product:
                    return reply("Один із товарів змінився в каталозі. Уточню замовлення для вас особисто.", human=True)
                if item["product"] not in stocks:
                    rows = stock_rows(self.inventory, product)
                    stocks[item["product"]] = stock_groups(rows)
                    guards[item["product"]] = stock_signature(rows)
        except StockUnavailable as error:
            notes.append(str(error))
            return reply("Зараз уточню наявність усіх речей і допоможу вам оформити замовлення 😊", human=True)
        demands = Counter()
        for item in cart:
            if item["size"] and item["color"]:
                demands[item["product"], item["size"], item["color"]] += item["quantity"]
        summary = "\n".join("%s. %s" % (i + 1, choice_text(self.catalog.products[item["product"]], item["size"], item["color"], item["quantity"])) for i, item in enumerate(cart))
        if advice_message:
            summary = advice_message + "\n\n" + summary
        for index, item in enumerate(cart):
            product, groups = self.catalog.products[item["product"]], stocks[item["product"]]
            context["cart_focus"] = index
            if not item["size"]:
                sizes = sorted({s for (s, c), amount in groups.items() if amount >= item["quantity"] and (not item["color"] or c == item["color"])}, key=lambda code: SIZES.index(code) if code in SIZES else 99)
                if not sizes:
                    return reply(summary + "\n\nДля %s уточню потрібну кількість і варіанти особисто." % product["name"], human=True)
                return reply(summary + "\n\n%s: який розмір обираєте? Зараз є: %s." % (product["name"], ", ".join(sizes)), "cart_size")
            if not item["color"]:
                colors = sorted({c for (s, c), amount in groups.items() if s == item["size"] and amount >= item["quantity"]})
                if colors:
                    return reply(summary + "\n\n%s, %s: який колір обираєте? Є: %s." % (product["name"], item["size"], ", ".join(COLORS.get(c, c) for c in colors)), "cart_color")
            needed = demands.get((item["product"], item["size"], item["color"]), item["quantity"])
            if not item["color"] or groups.get((item["size"], item["color"]), 0) < needed:
                rows = [{"size_code": s, "color_code": c, "available": amount} for (s, c), amount in groups.items()]
                choices = alternatives(product, rows, item["size"], item["color"], needed)
                body = "%s у потрібній кількості наразі немає." % choice_text(product, item["size"], item["color"])
                if choices:
                    body += " Є такі варіанти:\n" + "\n".join(choice_text(product, c["size"], c["color"], c["quantity"]) for c in choices)
                    if len(choices) == 1:
                        body += "\nБажаєте обрати цей варіант?"
                    else:
                        body += "\nЯкий розмір і колір вам підходить?"
                    sent["cart_pending"] = {"line": index, "choices": choices}
                    return reply(summary + "\n\n" + body, "cart_choice")
                return reply(summary + "\n\n" + body + " Уточню можливі варіанти особисто.", human=True)
        if "price" in flags or context.get("cart_order_requested"):
            prices = [self.catalog.products[item["product"]].get("price_uah") for item in cart]
            if all(price is not None for price in prices):
                summary += "\nРазом за товари: %s грн." % money(total_price(prices, cart))
            elif "price" in flags:
                return reply(summary + "\n\nУточню повну суму для всіх речей 😊", human=True)
        if context.get("cart_order_requested"):
            instructions = self.catalog.shop.get("order_instructions")
            if instructions:
                return reply("Усі вибрані речі є в наявності 😊\n" + summary + "\n\n" + instructions, "checkout", checkout=True)
            return reply(summary + "\n\nДопоможу оформити замовлення особисто 😊", human=True)
        return reply("Перевірив наявність 😊\n" + summary + "\n\nБажаєте оформити всі ці речі разом?", "cart_order")
