"""Small deterministic sales assistant. Facts come only from catalog/SQL.

Responses use catalog/SQL facts; optional delivery-directory reads precede confirmation.
"""

import copy
import re
from dataclasses import dataclass, field

from catalog import COLORS, SIZES, contains, normalize
from inventory import StockUnavailable, stock_signature, stock_rows
from customer_language import parameters, requested_size, requested_sizes, requested_color, requested_quantity, intents, reports_payment
from shop_facts import measurements_reply, shipping_reply, shop_day, alternatives, choice_text, garment_reference, money
from shopping_cart import ShoppingCart, YES
from checkout import Checkout
from customer_advice import advice_plan, append_fact_answers, fact_answers, FACT_TOPICS
from size_advice import asks_size_advice, height_advice
from conversation_flow import entry as conversation_entry
from customer_feedback import feedback_kind, APOLOGY
from customer_phrasing import canonical, customer_greeting
from customer_questions import question_plan
from selection_guard import guard_selection, slot_hint, invoice_variant
from olx_delivery import acknowledgment
from conversation_policy import requests_owner, abusive_only, autonomous_reply, next_step
from conversation_turn import prepare, information_plan, subject_guard, checkout_declaration, finish
import time


@dataclass
class Plan:
    text: str
    context: dict
    on_send: dict = field(default_factory=dict)
    guard: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


class Dialogue:
    def __init__(self, catalog, inventory, merchant=None, now=None, address_book=None):
        self.catalog, self.inventory = catalog, inventory
        self.merchant, self.now = merchant or {}, now
        self.address_book = address_book

    def plan(self, texts, saved, advert_id=None, has_attachments=False, delivery_notice=False):
        raw = "\n".join(str(t) for t in texts)
        original = raw
        saved = copy.deepcopy(saved)
        if saved.get('owner_request_reason') != 'customer':
            for key in ('owner_requested','handoff_now','owner_request_reason'):
                saved.pop(key,None)
            if saved.get('checkout_stage') == 'owner':
                if saved.get('invoice_issued'):
                    saved['checkout_stage'] = 'submitted' if saved.get('customer_confirmation') else 'details'
                else:
                    saved.pop('checkout_stage',None)
        texts = prepare(texts, saved, self.catalog)
        raw = "\n".join(str(t) for t in texts)
        if delivery_notice and not raw.strip():
            return Plan(acknowledgment(self.catalog.shop, self.now() if self.now else None), copy.deepcopy(saved),
                        {'greeted': True, 'olx_notice_received': True, 'olx_thanked_at': time.time()},
                        {'shipping_day': str(shop_day(self.catalog.shop, self.now() if self.now else None))})
        feedback = feedback_kind(raw, saved)
        action_flags = intents(canonical(raw))
        if feedback in ('negative','positive') and (action_flags.intersection({'order','price','dispatch','shipment_status','payment','measurements'}) or
                                                    asks_size_advice(raw,saved.get('awaiting'))):
            feedback = None
        human = requests_owner(raw)
        if abusive_only(raw) and not has_attachments and not human and feedback not in ('negative','positive'):
            result = Plan('Я на зв’язку. Якщо щось не так, напишіть, будь ласка, що саме — допоможу розібратися.', saved)
        elif human:
            result = self._dispatch(texts,saved,advert_id,has_attachments)
        elif feedback == 'action':
            result = Plan('Допоможу розібратися. Напишіть, будь ласка, що саме сталося з товаром і чи вже отримали посилку. За можливості додайте фото.', saved)
        elif feedback in ("negative", "positive"):
            body = APOLOGY if feedback == 'negative' else "Дуже тішуся, що вам сподобалося! Дякую за відгук 😊"
            result = Plan(body, copy.deepcopy(saved), {"feedback_acknowledged": True, "feedback_kind": feedback})
        else:
            information = information_plan(self, texts, saved, advert_id, has_attachments)
            subject = subject_guard(self, raw, saved, has_attachments)
            boundary = conversation_entry(raw, saved, self.catalog.adverts.get(str(advert_id)), has_attachments)
            if subject is not None:
                result = Plan(**subject)
            elif information is not None:
                result = Plan(**information)
            elif boundary and boundary.pop("continue", False):
                result = self._dispatch(texts, boundary["context"], advert_id, has_attachments)
                result.guard.update(boundary["guard"])
            else:
                result = Plan(**boundary) if boundary else self._dispatch(texts, saved, advert_id, has_attachments)
        if result.on_send.get('handoff_now') and not human:
            result.text = autonomous_reply(result,self.catalog,saved,raw,advert_id)
            result.on_send.pop('handoff_now',None)
            result.on_send.pop('owner_requested',None)
            if result.on_send.get('checkout_stage') == 'owner':
                result.on_send.pop('checkout_stage',None)
            if result.context.get('checkout_stage') == 'owner':
                result.context['checkout_stage'] = 'submitted' if saved.get('customer_confirmation') else 'details'
        if result.on_send.get('handoff_now'):
            result.on_send['owner_requested'] = True
            result.on_send['owner_request_reason'] = 'customer'
            if 'щойно звільниться' not in normalize(result.text):
                extra = '\n\nВласник відпише, щойно звільниться.'
                if len(result.text + extra) <= 2000:
                    result.text += extra
        if (delivery_notice and not result.on_send.get('olx_thanked_at') and
                not intents(canonical(raw)).intersection({'cancel', 'complaint'}) and
                time.time() - saved.get('olx_thanked_at', 0) >= 600):
            thanks = acknowledgment(self.catalog.shop, self.now() if self.now else None)
            if len(thanks + result.text) + 2 <= 2000:
                result.text = thanks + '\n\n' + result.text
                result.on_send.update(olx_notice_received=True, olx_thanked_at=time.time())
                result.guard['shipping_day'] = str(shop_day(self.catalog.shop, self.now() if self.now else None))
        salutation = customer_greeting(raw)
        if salutation:
            body = re.sub(r'^(?:(?:добрий|доброго|доброї|добрый)\s+\w+|привіт|вітаю|салам|салют|здравствуйте)[!.,…\s]+(?:[😊🙂]\s*)?', '', result.text, count=1, flags=re.I)
            if len(salutation + ' ' + body) <= 2000:
                result.text = salutation + ' ' + body
            result.on_send['greeted'] = True
        elif not saved.get("greeted"):
            if not re.match(r"(?:добрий|доброго|доброї|привіт|вітаю|салам|здравствуйте|добрый)\b", result.text, re.I):
                greeting = "Добрий день! "
                if len(greeting + result.text) <= 2000:
                    result.text = greeting + result.text
            result.on_send["greeted"] = True
        return finish(result, saved, original)

    def _dispatch(self, texts, saved, advert_id=None, has_attachments=False):
        if len(texts) > 20 or sum(len(str(t)) for t in texts) > 6000:
            return self._plan(texts, saved, advert_id, has_attachments)
        raw = "\n".join(str(t) for t in texts)
        if saved.get('owner_requested') and re.search(r'(?:коли|когда|де|где).{0,30}(?:власник|оператор|хозяин)|(?:власник|оператор|хозяин).{0,30}(?:коли|когда|відпов|ответ)', normalize(raw)):
            return Plan('Власник відпише, щойно звільниться.', copy.deepcopy(saved))
        human = requests_owner(raw)
        identity = re.compile(r"\b(?:ти|ви|ты|вы|це|это)\s+(?:не\s+)?(?:(?:живий|жива|живой|реальний|настоящий)\s+)?(?:бот|робот|людина|человек)\b|\b(?:бот|робот)\s+(?:чи|или)\s+(?:людина|человек)\b|\bare\s+you\s+(?:a\s+)?(?:bot|human)\b|^(?:бот|робот)\s*\?", re.I)
        if identity.search(raw):
            context = copy.deepcopy(saved)
            owner = self.catalog.shop.get("owner_telegram", "@boy_in_white")
            is_bot_question = bool(re.search(r"\bбот|робот|\bbot\b", normalize(raw)))
            intro = ("Так 🙂 Я" if is_bot_question else "Я") + " Repliq — автоматизований помічник магазину. Мене створила одна людина, щоб допомагати з вибором товарів та оформленням замовлень."
            contact = "Мій автор і власник магазину — %s. Якщо хочете поспілкуватися з ним, напишіть «поклич власника» — передам йому цей діалог." % owner
            remainder = identity.sub("", raw).strip(" ,.!?—\n")
            if not human and intents(normalize(remainder)).difference({"greeting", "thanks"}):
                result = self._plan([remainder], context, advert_id, has_attachments)
                result.text = re.sub(r'^(?:добрий\s+день|привіт|вітаю)[!.,\s]+(?:[😊🙂]\s*)?', '', result.text, count=1, flags=re.I)
                result.text = "Я Repliq — автоматизований помічник, якого створила одна людина: %s.\n\n" % owner + result.text
                if len(result.text) <= 2000:
                    return result
            return Plan(intro + "\n\n" + ("Передаю йому цей діалог — він відповість особисто. Контакт: " + owner if human else contact),
                        context, {"greeted": True, "handoff_now": human})
        if human:
            return Plan("Звісно, передам ваше звернення власнику. Він відпише, щойно звільниться.",
                        copy.deepcopy(saved), {"greeted": True, "handoff_now": True})
        if re.search(r'контакт|телефон|номер',canonical(raw)) and re.search(r'власник|продав|хазяїн|хозяин|автор',canonical(raw)):
            owner=self.catalog.shop.get('owner_telegram','@boy_in_white')
            return Plan('З власником можна зв’язатися в Telegram — '+owner+'. Я також можу допомогти з вашим запитанням тут.',copy.deepcopy(saved))
        if re.search(r'хто\s+(?:ти|ви|тебе\s+створив|(?:твій|ваш)\s+(?:автор|власник))|хто\s+(?:власник|автор)|как\s+тебя\s+зовут|як\s+тебе\s+звати',canonical(raw)):
            owner=self.catalog.shop.get('owner_telegram','@boy_in_white')
            return Plan('Я Repliq — автоматизований помічник магазину. Мене створила одна людина. Власник магазину — '+owner+'. Допоможу з вибором та оформленням.',copy.deepcopy(saved))
        if re.fullmatch(r"(?:ясно|понятно|зрозуміло|хаха[ха]*|аха[ха]*|ахах[аха]*|[😂🤣👍🙂]+)[.! ]*", normalize(raw)):
            return Plan("Добре, я на зв’язку 🙂", copy.deepcopy(saved))
        return self._plan(texts, saved, advert_id, has_attachments)

    def _plan(self, texts, saved, advert_id=None, has_attachments=False, answer_questions=True):
        context = copy.deepcopy(saved)
        if len(texts) > 20 or sum(len(str(t)) for t in texts) > 6000:
            return Plan("Дякую за деталі! Уважно перегляну ваше звернення, щоб відповісти точно.", context,
                        on_send={"handoff_now": True},
                        notes=["Велике звернення: прочитай повний діалог в OLX і відредагуй відповідь."])
        raw = "\n".join(str(t) for t in texts)
        text = "\n".join(canonical(line) for line in raw.splitlines())[:6000]
        flags = intents(text)
        protected = guard_selection(self.catalog, self.inventory, text, context, advert_id, flags, has_attachments)
        if protected is not None:
            return Plan(**protected)
        existing_variant = invoice_variant(self.catalog, self.inventory, text, context, flags, has_attachments)
        if existing_variant is not None:
            return Plan(**existing_variant)
        declaration = checkout_declaration(self, raw, context, flags, has_attachments)
        if declaration is not None:
            return Plan(**declaration)
        if answer_questions:
            question = question_plan(self.catalog, self.merchant, raw, context, advert_id, flags,
                                     self.now() if self.now else None, has_attachments)
            if question is not None:
                has_selection = requested_size(text) or requested_color(text) or requested_quantity(text)
                if flags.intersection({'order', 'availability'}) and has_selection and not question['on_send'].get('handoff_now'):
                    action = self._plan(texts, context, advert_id, has_attachments, answer_questions=False)
                    if question['text'] not in action.text and len(action.text + question['text']) + 2 <= 2000:
                        action.text += '\n\n' + question['text']
                        action.guard.update(question['guard'])
                    return action
                extra = information_plan(self, texts, context, advert_id, has_attachments, allow_mixed=True)
                if extra is not None and extra['text'] not in question['text']:
                    combined = question['text'] + '\n\n' + extra['text']
                    if len(combined) <= 2000:
                        question['text'] = combined
                        question['guard'].update(extra['guard'])
                return Plan(**question)
        else:
            flags.discard('arrival')
        if context.get("advice_pending") and (flags.intersection({"order", "cancel", "complaint", "availability", "price", "dispatch", "payment", "measurements"}) or
                                              re.search(r"додай|добав|прибери|убери", text)):
            context.pop("advice_pending", None)
        if flags:
            context["clarifications"] = 0
        if context.get('checkout_delivery_prepay') and context.get('invoice_issued'):
            carrier = Checkout(self.catalog, self.inventory, self.merchant, self.now, self.address_book).plan(raw, context, flags, has_attachments)
            if carrier is not None:
                return Plan(**append_fact_answers(carrier, self.catalog, text, flags, advert_id))
        advice = advice_plan(self.catalog, text, context, advert_id, flags, self.now() if self.now else None, has_attachments)
        if advice is not None:
            return Plan(**advice)
        checkout = Checkout(self.catalog, self.inventory, self.merchant, self.now, self.address_book).plan(raw, context, flags, has_attachments)
        if checkout is not None:
            return Plan(**append_fact_answers(checkout, self.catalog, text, flags, advert_id))
        if reports_payment(text) and not context.get("checkout_offered") and not context.get("invoice_issued"):
            context.setdefault("product", self.catalog.adverts.get(str(advert_id)))
            return Plan("Дякую, що повідомили. Уточнимо ваше замовлення та перевіримо оплату особисто.", context,
                        {"handoff_now": True, "greeted": True})
        cart = ShoppingCart(self.catalog, self.inventory).plan(text, context, advert_id, flags)
        if cart is not None:
            return Plan(**append_fact_answers(cart, self.catalog, text, flags, advert_id))
        named = self.catalog.explicit_products(text)
        parse_product = named[0] if len(named) == 1 else self.catalog.products.get(context.get("product") or self.catalog.adverts.get(str(advert_id)), {})
        values, errors = parameters(text, context, weight_for_fit=parse_product.get("sizing_basis") != "height")
        # A bare garment pair (45/65) is not a person's height and weight.
        if saved.get("awaiting") in ("garment_measurements", "usual_size") and garment_reference(text) and not re.search(r"зріст|ріст|рост|ваг|вес|важу", text):
            values, errors = {}, []
        size, color = requested_size(text), requested_color(text)
        if 'measurements' in flags and 'order' not in flags:
            size, color = None, None
        explicit = self.catalog.explicit_products(text)
        notes, guard = [], {}
        if re.search(r"для\s+(?:інш|друг|брат|сестр|чоловік|дружин|дитин|реб[её]н|сина|сына|доньк|дочер|мам|тат|отц|матер)|не\s+для\s+(?:мене|меня)", text):
            for key in ("height", "weight", "size", "fit", "weight_followup_asked", "weight_declined", "size_advice_requested"):
                context.pop(key, None)
        greeting = "Добрий день! 😊 " if ("greeting" in flags or "availability" in flags) and not context.get("greeted") else ""
        human_needed = False

        def result(body, awaiting=None):
            context["awaiting"] = awaiting
            response = (greeting + body).strip()
            if len(response) > 2000:
                notes.append("Забагато інформації для однієї відповіді: відредагуй чернетку.")
                response = greeting + "Дякую за запитання! Уточню деталі, щоб відповісти вам точно."
            if response.count("😊") > 1:
                first = response.index("😊") + 1
                response = response[:first] + response[first:].replace("😊", "").rstrip()
            sent = {"greeted": True, "awaiting": awaiting}
            if human_needed:
                sent["handoff_now"] = True
            return Plan(response, context, sent, guard, notes)

        if "dispatch" in flags and not flags.intersection({"availability", "order", "sizing", "price"}):
            guard["shipping_day"] = str(shop_day(self.catalog.shop, self.now() if self.now else None))
            return result(shipping_reply(self.catalog.shop, self.now() if self.now else None), context.get("awaiting"))
        if "arrival" in flags:
            human_needed = True
            return result("Термін у дорозі залежить від перевізника й населеного пункту. Підкажіть, будь ласка, куди потрібна доставка — уточнимо для вас.")
        pending = context.pop("pending_alternatives", None)
        if pending and YES.fullmatch(text):
            if len(pending) != 1:
                context["pending_alternatives"] = pending
                return result("Який саме розмір і колір обираєте? Підкажіть, будь ласка 😊", "alternative")
            choice = pending[0]
            context.update({k: choice[k] for k in ("product", "size", "color", "quantity")})
            size, color = choice["size"], choice["color"]
            flags.add("availability")

        if "cancel" in flags:
            context.pop("order_requested", None)
            context["topic"] = "closed"
            return result("Зрозуміло, дякую, що повідомили. Якщо виникнуть запитання — звертайтеся!")
        if "complaint" in flags:
            human_needed = True
            notes.append("Потрібна особиста відповідь власника; автоматична консультація зупинена.")
            return result("Дякую, що написали. Потрібно перевірити деталі вашого звернення — уточню їх особисто.")
        if len(explicit) > 1:
            context["product"] = None
            return result("Підкажіть, будь ласка, про який товар зараз говоримо: " +
                          " чи ".join(p["name"] for p in explicit) + "?", "product")
        selected = explicit[0]["key"] if explicit else context.get("product") or self.catalog.adverts.get(str(advert_id))
        if selected != context.get("product"):
            for key in ("size", "color", "fit", "topic", "quantity", "size_advice_requested", "weight_followup_asked"):
                context.pop(key, None)
            context["product"] = selected
        context.update(values)
        if size:
            context["size"] = size
        if color:
            context["color"] = color
        quantity = requested_quantity(text)
        if quantity is not None:
            amount = quantity
            if not 1 <= amount <= 20:
                notes.append("Нестандартна кількість — узгодь особисто.")
                return result("Підкажіть, будь ласка, які саме розміри та кольори вам потрібні? Перевірю можливість такого замовлення.", "quantity")
            context["quantity"] = amount
        if flags.intersection({"availability", "order", "sizing"}) or size or color:
            context.pop("checkout_offered", None)
            context.pop("checkout_stage", None)
            context.pop("pending_checkout_method", None)
        if re.search(r"звичайн|обычн|стандарт|regular|по\s+фігур|по\s+фигур|не\s+хочу\s+оверсайз", text):
            context["fit"] = "regular"
        elif re.search(r"оверсайз|oversiz|вільн|свободн|пошир|пошыр", text):
            context["fit"] = "loose"
        if errors:
            return result(errors[0], "parameters")
        if not text:
            notes.append("Вкладення без тексту: потрібно переглянути його в OLX.")
            return result(("Вкладення отримав. " if has_attachments else "") + "Підкажіть, будь ласка, що саме вас цікавить щодо товару?", "question")
        if "thanks" in flags and not flags.difference({"thanks", "greeting"}) and not (size or color or values):
            return result("Немає за що 😊", context.get("awaiting"))
        if "greeting" in flags and not flags.difference({"greeting"}) and not (size or color or values):
            return result("Бажаєте замовити чи допомогти з вибором?", "order")
        if contains(text, "так") or contains(text, "да") or contains(text, "ага"):
            if context.get("awaiting") == "order" and not flags.intersection({"sizing", "measurements"}):
                flags.add("order")
        if "order" in flags:
            context["order_requested"] = True
        wants_advice = asks_size_advice(text, saved.get("awaiting"))
        pending_advice = context.get("size_advice_requested") or (selected == saved.get("product") and saved.get("topic") == "sizing" and saved.get("awaiting") in ("parameters", "height", "weight", "fit", "garment_measurements", "usual_size"))
        if size and not wants_advice:
            context["size_advice_requested"] = pending_advice = False
        if wants_advice:
            if not pending_advice:
                context.pop("weight_followup_asked", None)
            context["size_advice_requested"] = True
            if parse_product.get('sizing_basis') == 'height' or parse_product.get('size_chart_verified'):
                size = None
        if wants_advice or pending_advice:
            context["topic"] = "sizing"
        elif "order" in flags:
            context["topic"] = "order"
        elif "availability" in flags or "sizing" in flags or size or color or values:
            context["topic"] = "availability"
        product = self.catalog.products.get(context.get("product"))
        if not product:
            names = [p["name"] for p in self.catalog.products.values()][:6]
            return result("Підкажіть, будь ласка, який товар вас цікавить" +
                          (": " + ", ".join(names) if names else "") + "?", "product")
        if size and size in product.get("excluded_sizes", []):
            if saved.get("size") and saved["size"] not in product["excluded_sizes"]:
                context["size"] = saved["size"]
            else:
                context.pop("size", None)
            return result("%s у розмірі %s не продаємо. Можемо підібрати інший розмір." % (product["name"], size), "size")

        if product.get("sizing_basis") != "height" and saved.get("awaiting") in ("garment_measurements", "usual_size") and not size and not flags.intersection({"measurements", "price", "delivery", "payment", "photos"} | FACT_TOPICS):
            reference = garment_reference(text) or context.get("garment_reference", {})
            context["garment_reference"] = reference
            if "width_cm" not in reference or "length_cm" not in reference:
                if values or re.search(r"не\s+(?:можу|могу|знаю)|немає\s+можливості", text):
                    prefix = "Зріст і вагу записав. " if context.get("height") and context.get("weight") else "Нічого страшного, можна без замірів. "
                    return result(prefix + "Який розмір футболок зазвичай носите — S, M чи L? І яку посадку любите: звичайну чи вільнішу?", "usual_size")
                if saved.get("awaiting") == "usual_size":
                    return result("Можемо почати зі звичного розміру футболок — S, M чи L. Якщо не знаєте, напишіть: підберемо інший спосіб порівняти посадку.", "usual_size")
                return result("Можна написати ширину й довжину зручної футболки, наприклад «45/65», або просто свій звичний розмір — S, M чи L.", "garment_measurements")
            matches = []
            for code, measured in product.get("garment_measurements", {}).items():
                if all(k in measured and abs(measured[k] - reference[k]) <= 2 for k in ("width_cm", "length_cm")):
                    matches.append((sum(abs(measured[k] - reference[k]) for k in reference), code))
            if not matches:
                human_needed = True
                return result("Дякую за заміри! Вони відрізняються від нашої сітки — уточнимо посадку, щоб не порадити навмання.")
            best = min(score for score, _ in matches)
            choices = [code for score, code in matches if score == best]
            if len(choices) > 1 and not context.get("fit"):
                return result("Найближчі за замірами розміри %s. Яку посадку любите — звичайну чи вільнішу?" % " / ".join(choices), "garment_measurements")
            choices.sort(key=SIZES.index)
            size = choices[-1] if context.get("fit") == "loose" else choices[0]
            context["size"], context["topic"] = size, "availability"
            flags.discard("measurements")
            flags.discard("sizing")
            flags.add("availability")

        parts = []
        fact_flags = flags - {"fit"} if saved.get("awaiting") == "fit" and not re.search(r"\?|крій|посадк|як\b|како", text) else flags
        facts, missing_facts = fact_answers(self.catalog, product, text, fact_flags)
        parts.extend(facts)
        if missing_facts:
            parts.append("Уточню для вас " + ", ".join(missing_facts) + ".")
            human_needed = True
            notes.extend(missing_facts)
        if (product.get("waist_measurement_kind") == "unspecified" and
                re.search(r"обхват|по\s+колу|напівобхват|півобхват|полуобхват", text)):
            human_needed = True
            return result("Заміри пояса в нас є, але спосіб вимірювання ще потрібно уточнити. Перевіримо це для вас, щоб правильно порівняти посадку.")
        if "dispatch" in flags:
            parts.append(shipping_reply(self.catalog.shop, self.now() if self.now else None))
            guard["shipping_day"] = str(shop_day(self.catalog.shop, self.now() if self.now else None))
        for intent in ("price", "delivery", "payment", "returns", "measurements", "authenticity"):
            if intent not in flags:
                continue
            if intent == "delivery" and "dispatch" in flags:
                continue
            if intent == "measurements":
                body, missing = measurements_reply(product, requested_sizes(text) or ([context["size"]] if context.get("size") else None))
                parts.append("Ось заміри в сантиметрах:\n" + body if body else "Уточню точні заміри для вас.")
                human_needed |= missing
                continue
            if intent == "price":
                price = product.get("price_uah")
                if price is not None:
                    parts.append("Ціна — %s грн." % money(price))
                else:
                    parts.append("Актуальну ціну уточню для вас.")
                    human_needed = True
                    notes.append("Заповни price_uah для цього товару або відредагуй відповідь.")
            else:
                value = product.get(intent) if intent in ("description", "measurements", "authenticity") else self.catalog.shop.get(intent)
                if value:
                    parts.append(value)
                else:
                    labels = {"delivery": "умови доставки", "payment": "умови оплати", "returns": "умови обміну та повернення",
                              "discount": "можливість знижки", "description": "склад і характеристики тканини",
                              "measurements": "точні заміри виробу", "authenticity": "інформацію про походження товару"}
                    parts.append("Уточню для вас %s." % labels[intent])
                    human_needed = True
                    notes.append("Відсутній факт у shop.json: " + intent)
        if "photos" in flags:
            human_needed = True
            parts.append("Уточніть, будь ласка, який ракурс або деталь хочете побачити?")
            notes.append("Фото потрібно додати вручну в OLX; Repliq не вигадує зображення.")

        topic = context.get("topic")
        needs_stock = bool(flags.intersection({"availability", "sizing", "order"}) or wants_advice or pending_advice or size or color or values or
                           (saved.get("awaiting") in ("parameters", "height", "weight", "fit") and not parts))
        if "measurements" in flags and not flags.intersection({"availability", "order"}):
            return result("\n\n".join(parts), context.get("awaiting"))
        recommendation = None
        fitted_by_parameters = False
        if (needs_stock and topic == "sizing" and not size and context.get("size") and
                product["kind"] == "tee" and not product["size_chart_verified"] and product.get("sizing_basis") != "height"):
            size = context["size"]
            measured, missing = measurements_reply(product, [size])
            if measured:
                parts.append("Ви обрали %s. Щоб оцінити посадку, ось його заміри:\n%s" % (size, measured))
                human_needed |= missing
        if needs_stock and topic == "sizing" and not size and product.get("sizing_basis") == "height":
            advice = height_advice(product, text, context)
            if advice.get("question"):
                human_needed |= advice.get("human", False)
                prefix = 'Ви обрали %s. ' % saved['size'] if saved.get('size') and not context.get('height') else ''
                return result(" ".join(parts + [prefix + advice["question"]]), advice.get("awaiting"))
            recommendation = advice["size"]
            context["size"] = recommendation
            fitted_by_parameters = "height"
        elif needs_stock and topic == "sizing" and not size:
            guide = product.get("height_guidance", {}) if not product["size_chart_verified"] else {}
            if not product["size_chart_verified"] and not guide and product["kind"] == "tee" and product.get("garment_measurements"):
                prefix = "Дякую, зріст і вагу записав. " if context.get("height") and context.get("weight") else "Допоможу 😊 "
                return result(prefix + "Який розмір футболок зазвичай носите — S, M чи L? Якщо маєте заміри зручної футболки, можете написати ширину й довжину — порівняємо.", "usual_size")
            if not context.get("height") or not context.get("weight") and not guide:
                if not context.get("height") and not context.get("weight"):
                    question, awaiting = "Підкажіть, будь ласка, ваш зріст і вагу — допоможу підібрати розмір.", "parameters"
                elif not context.get("height"):
                    question, awaiting = "Дякую! Підкажіть ще, будь ласка, ваш зріст.", "height"
                else:
                    question, awaiting = "Дякую! Підкажіть ще, будь ласка, вашу вагу.", "weight"
                return result(" ".join(parts + [question]), awaiting)
            if not product["size_chart_verified"] and not guide:
                measurements, missing = measurements_reply(product)
                if product["kind"] == "tee" and measurements:
                    return result("Дякую! За зростом і вагою посадку цієї моделі точно не визначити.\n" + measurements +
                                  "\n\nПідкажіть ширину й довжину вашої зручної футболки — порівняємо заміри 😊", "garment_measurements")
                human_needed = True
                return result("Дякую за параметри. Уточнимо заміри цієї моделі, щоб підібрати розмір точніше.")
            candidates = [s for s, limits in guide.items() if limits[0] <= context["height"] <= limits[1]] if guide else [r["size"] for r in product["sizes"]
                          if r["height_cm"][0] <= context["height"] <= r["height_cm"][1]
                          and r["weight_kg"][0] <= context["weight"] <= r["weight_kg"][1]]
            if not candidates:
                human_needed = True
                notes.append("Параметри поза таблицею: потрібен підбір власником за замірами.")
                if not product["sizes"]:
                    notes.append("Для нового товару з Ledgerly ще немає перевіреної розмірної сітки.")
                    return result(" ".join(parts + ["Для цієї моделі потрібно звірити точні заміри. Уточню їх для вас, щоб підібрати розмір правильно."]))
                return result(" ".join(parts + ["За цими параметрами краще звірити точні заміри, щоб не помилитися з розміром. Яку посадку ви любите — звичайну чи вільнішу?"]), "fit")
            candidates.sort(key=SIZES.index)
            if len(candidates) > 1 and not context.get("fit"):
                prefix = "За зростом можна орієнтовно розглянути" if guide else "Ваші параметри на межі розмірів"
                return result(" ".join(parts + [prefix + " %s. Вам більше подобається звичайна чи вільніша посадка?" % " / ".join(candidates)]), "fit")
            recommendation = candidates[-1] if context.get("fit") == "loose" else candidates[0]
            context["size"] = recommendation
            fitted_by_parameters = "height" if guide else "body"
            if guide:
                notes.append("Орієнтир за зростом приблизний; вага не перетворюється на розмір без перевіреної сітки.")
        elif needs_stock:
            recommendation = context.get("size")

        if needs_stock:
            try:
                rows = stock_rows(self.inventory, product)
                guard.update(product=product["key"], stock=stock_signature(rows))
            except StockUnavailable as error:
                human_needed = True
                notes.append(str(error))
                body = "Наявність зараз потрібно уточнити — не хочу обіцяти вам товар без перевірки."
                return result(" ".join(parts + [body]), "stock")
            matching = [r for r in rows if (not context.get("color") or r["color_code"] == context["color"])
                        and (not recommendation or r["size_code"] == recommendation)]
            amount = context.get("quantity", 1)
            available = [r for r in matching if r["available"] >= amount]
            name = product["name"]
            color_label = COLORS.get(context.get("color"), "")
            choice = name + (" у розмірі " + recommendation if recommendation else "") + (", " + color_label + " колір" if color_label else "")
            if not available:
                choices = alternatives(product, rows, recommendation, context.get("color"), amount)
                if choices:
                    body = "%s зараз немає в наявності." % choice
                    if recommendation and context.get('color') and all(item['size'] == recommendation for item in choices):
                        labels = list(dict.fromkeys(COLORS.get(item['color'], item['color']) for item in choices))
                        asked_color = {'BLUE':'Синій', 'PURPLE':'Фіолетовий', 'GREEN':'Зелений', 'RED':'Червоний', 'BEIGE':'Бежевий'}.get(context.get('color'))
                        body = ('%s колір у розмірі %s зараз не в наявності. ' % (asked_color, recommendation) if asked_color and amount == 1 else
                                'У розмірі %s цього кольору зараз немає. ' % recommendation if amount == 1 else
                                'У розмірі %s потрібної кількості цього кольору зараз немає. ' % recommendation)
                        body += 'Зараз є: ' + ', '.join(labels) + '.'
                        body += ' Бажаєте цей колір?' if len(labels) == 1 else ' Який колір обираєте?'
                        plan = result('\n\n'.join(parts + [body]), 'alternative')
                        plan.on_send['pending_alternatives'] = choices
                        return plan
                    body += "\nЗараз є: " + "; ".join(choice_text(product, item["size"], item["color"], item["quantity"]) for item in choices) + "."
                    if len(choices) == 1 and choices[0]["size"] != recommendation:
                        measurements, _ = measurements_reply(product, [choices[0]["size"]])
                        if measurements:
                            body += "\n" + measurements
                    body += "\nБажаєте обрати цей варіант?" if len(choices) == 1 else "\nЯкий варіант вам підходить?"
                    plan = result("\n\n".join(parts + [body]), "alternative")
                    plan.on_send["pending_alternatives"] = choices
                    return plan
                other_sizes = sorted({r["size_code"] for r in rows if r["available"] >= amount
                                      and (not context.get("color") or r["color_code"] == context["color"])},
                                     key=lambda v: SIZES.index(v) if v in SIZES else 99)
                body = ("%s зараз немає в наявності." % choice if amount == 1 else
                        "%s у кількості %s шт. зараз недоступні." % (choice, amount))
                if other_sizes:
                    body += " Є розміри %s." % ", ".join(other_sizes)
                    body += " Можу надіслати точні заміри, щоб порівняти посадку."
                else:
                    body += " Можу допомогти перевірити інший колір або товар."
                return result(" ".join(parts + [body]), "alternative")
            if recommendation and fitted_by_parameters:
                parts.append("Вам підійде %s. Цей розмір є в наявності." % recommendation)
                if product.get("sizing_basis") == "height":
                    context["topic"] = "availability"
            elif recommendation:
                if saved.get('product') == product['key'] and saved.get('greeted'):
                    short = (color_label.capitalize() + ' колір у розмірі ' + recommendation if color_label else 'Розмір ' + recommendation)
                    parts.append(short + ' є в наявності.')
                else:
                    spoken_choice = choice[0].lower() + choice[1:] if re.match(r"Штани|Футболка|Джинси|Кофта|Куртка", choice) else choice
                    parts.append("Так, %s є в наявності." % spoken_choice)
            else:
                sizes = sorted({r["size_code"] for r in available}, key=lambda v: SIZES.index(v) if v in SIZES else 99)
                intro = "Так, актуально." if re.search(r"актуальн|ще\s+прода|не\s+продан", text) else "Так,"
                spoken_name = name[0].lower() + name[1:] if re.match(r"Штани|Футболка|Джинси|Кофта|Куртка", name) else name
                parts.append("%s %s є в наявності. Доступні розміри: %s." % (intro, spoken_name, ", ".join(sizes)))
            if not recommendation:
                return result(" ".join(parts + ["Який розмір бажаєте?"]), "size")
            if not context.get("color"):
                colors = list(dict.fromkeys(COLORS.get(r["color_code"], "інший") for r in available))
                return result(" ".join(parts + ["Який колір вам подобається? Зараз є: %s." % ", ".join(colors)]), "color")
            if topic == "order" or "order" in flags or context.get("order_requested"):
                instructions = self.catalog.shop.get("order_instructions")
                if instructions:
                    parts.append(instructions)
                else:
                    parts.append("Підкажіть, будь ласка, який спосіб доставки вам зручний?")
                    notes.append("Узгодь оформлення особисто. Бот не створює замовлення й не резервує товар.")
                plan = result("\n\n".join(parts), "checkout")
                if instructions and len(greeting + "\n\n".join(parts)) <= 2000:
                    plan.on_send["checkout_offered"] = True
                return plan
            return result(" ".join(parts + ["Бажаєте оформити замовлення?"]), "order")
        if parts:
            return result(" ".join(parts))
        hint = slot_hint(self.catalog, self.inventory, context, advert_id)
        if hint is not None:
            return Plan(**hint)
        context["clarifications"] = saved.get("clarifications", 0) + 1
        return result(next_step(self.catalog,context,advert_id), saved.get('awaiting') or 'question')
