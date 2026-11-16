"""Payment instructions and shipping details. Receipt verification stays with the owner."""

import copy
import hashlib
import json
import re
import uuid
from collections import Counter
from pathlib import Path

from app import AppError, read_json
from catalog import normalize
from inventory import StockUnavailable, stock_signature, stock_rows
from shop_facts import stock_groups, shipping_reply, shop_day, money, total_price, measurements_reply, FULL_PREPAYMENT_REFUND, COD_DEPOSIT_REFUND, COD_WITHOUT_PREPAYMENT
from order_summary import snapshot, summary, confirmation_key, order_items, invoice_key, restore_legacy_invoice, new_order_number
from carrier_payment import enabled as carrier_enabled, direct_delivery
from customer_language import requested_sizes, requested_quantity, requested_color, reports_payment
from checkout_changes import quantity_change
from size_advice import asks_size_advice, height_advice, preferred_available_size
from customer_contact import contact_details, confirms, close_word
from customer_phrasing import canonical, cod_mentioned, cod_deposit_question

DETAILS = "ПІБ отримувача:\nНомер телефону:\nМісто або село, область:\nВідділення або поштомат НП, номер:"
LABELS = {"name": "ПІБ", "phone": "Номер телефону", "place": "Місто або село, область", "branch": "Номер відділення"}


def requests_invoice(text):
    text = canonical(text)
    return bool(re.search(r"реквіз|реквиз|рахун|сч[её]т|номер\s+карт|(?:куди|куда|як|как)\s+(?:платити|платить|оплатити|оплатить)|(?:скинь|кидай|скидай|надішли|пришли).{0,15}(?:картк|карту|iban)", text) or
                re.fullmatch(r'(?:(?:а|і|и|ще|еще)\s+)?(?:карту|картку|карта|картка|iban)[?!. ]*', text))


def declares_receipt(text):
    if "?" in text or re.search(r"\b(?:скину|скидатиму|надішлю|пришлю|отправлю|пізніше|потім|потом)\b", text):
        return False
    return any(not re.search(r"\bне\s*$", text[max(0, match.start() - 10):match.start()])
               for match in re.finditer(r"квитанц\w*|квитанці\w*|\bчек\b|це\s+оплата|это\s+оплата", text))


def missing_details_prompt(details):
    missing = [label + ":" for key, label in LABELS.items() if not details.get(key)]
    if not missing:
        return 'Дані для відправки вже записані.'
    if len(missing) == 1:
        field = next(key for key in LABELS if not details.get(key))
        return {"name": "Решту даних зберіг. Підкажіть лише ПІБ отримувача, будь ласка.",
                "phone": "Решту даних зберіг. Напишіть лише повний номер телефону: 10 цифр, починаючи з 0, або у форматі +380…",
                "place": "Решту даних зберіг. Уточніть лише населений пункт і область; для села — також район.",
                "branch": "Решту даних зберіг. Підкажіть лише тип і номер НП — наприклад, «69 відділення» або «поштомат №12345»."}[field]
    return "Надішліть, будь ласка, решту даних одним повідомленням — можна скопіювати й заповнити:\n" + "\n".join(missing)


def valid_iban(value):
    if not isinstance(value, str) or not re.fullmatch(r'UA[0-9]{27}', value):
        return False
    rotated = value[4:] + value[:4]
    digits = ''.join(str(ord(char)-55) if char.isalpha() else char for char in rotated)
    return int(digits) % 97 == 1


def payment_settings(data_dir, with_bank=True):
    data = read_json(Path(data_dir) / "payments.json")
    if not data.get("enabled", False):
        return {}
    kind = data.get("kind")
    value = re.sub(r"\s", "", str(data.get("account", "")))
    if (kind not in ("card", "iban") or
            kind == "card" and not re.fullmatch(r"\d{16,19}", value) or
            kind == "iban" and not valid_iban(value) or
            not isinstance(data.get("recipient", ""), str) or len(data.get("recipient", "")) > 150):
        raise AppError("Перевір private/payments.json: тип card/iban, номер картки або IBAN та отримувача.")
    if 'bank_name' in data and (not isinstance(data['bank_name'], str) or not 1 <= len(data['bank_name']) <= 100):
        raise AppError('У private/payments.json поле bank_name має містити назву банку.')
    merchant = dict(data, account=value)
    if with_bank:
        from monobank_payments import bank_marker
        automatic = bank_marker(data_dir, merchant)
        if automatic:
            merchant['_monobank'] = automatic
    return merchant


def payment_signature(data):
    return hashlib.sha256(json.dumps({k:v for k,v in data.items() if k != '_monobank'}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def method_choice(text):
    text = canonical(text)
    words = {'one':'1','first':'1','перший':'1','першим':'1','першу':'1','первый':'1','первым':'1','один':'1',
             'two':'2','second':'2','другий':'2','другим':'2','другу':'2','второй':'2','вторым':'2','два':'2','дві':'2',
             'three':'3','third':'3','третій':'3','третім':'3','третю':'3','третий':'3','три':'3'}
    text = re.sub(r'\b(?:'+ '|'.join(words) +r')\b',lambda m:words[m[0]],text)
    text = re.sub(r'^(?:давай(?:те)?|обираю|вибираю|беру|хочу|выбираю)\s+','',text)
    text = re.sub(r'\s*(?:будь\s+ласка|пожалуйста|please)[.! ]*$','',text).strip(' ,.!')
    text = re.sub(r'(?<=\d)[ -]?(?:й|ий|ій|им|ім)(?!\w)','',text)
    number = re.fullmatch(r"(?:(?:варіант|вариант|спосіб|способ|номер)\s*)?([123])(?:\s*(?:варіант|вариант|спосіб|способ))?[.!]?", text)
    if number:
        return {"1": "olx", "2": "cod", "3": "full"}[number[1]]
    olx = bool(re.search(r"(?:олх|олікс|olx)", text))
    cod = cod_mentioned(text) or bool(re.search(r"(?:передоплат|передплат).{0,15}100|100\s*(?:грн|грив)", text))
    if not cod:
        forms = ("післяплата", "післяплатою", "післяплатой", "послеплата", "послеплатой", "наложкою", "наложкой")
        for match in re.finditer(r"[а-яіїєґё]{7,16}", text):
            if (not re.search(r"\bне\s+(?:хочу\s+)?$", text[max(0, match.start() - 20):match.start()]) and
                    any(close_word(match[0], form) for form in forms)):
                cod = True
                break
    full = bool(re.search(r"повн\w*\s+(?:передоплат|передплат|оплат)|полн\w*\s+(?:предоплат|оплат)|всю\s+сум|вся\s+сум|одразу\s+вс[ею]", text))
    if re.search(r"не\s+(?:через\s+)?(?:олх|olx)", text):
        olx = False
    if re.search(r"не\s+(?:хочу\s+)?(?:налож|післяплат|накладен|накладн)", text):
        cod = False
    methods = [name for name, found in (("olx", olx), ("cod", cod), ("full", full)) if found]
    return methods[0] if len(methods) == 1 else "ambiguous" if methods else None


def shipping_details(raw, saved, awaiting=None):
    return contact_details(raw, saved, awaiting)


def payment_details(merchant, amount, full_prepayment=False):
    account = merchant["account"]
    number = " ".join(account[i:i + 4] for i in range(0, len(account), 4)) if merchant["kind"] == "card" else account
    label = "Картка" if merchant["kind"] == "card" else "Рахунок (IBAN)"
    recipient = "\nОтримувач: " + merchant["recipient"] if merchant.get("recipient") else ""
    text = label + ":\n" + number + recipient + "\nСума: " + str(amount) + " грн."
    return text + '\n\n' + (FULL_PREPAYMENT_REFUND if full_prepayment else COD_DEPOSIT_REFUND)


class Checkout:
    def __init__(self, catalog, inventory, merchant, now=None, address_book=None):
        self.catalog, self.inventory, self.merchant = catalog, inventory, merchant
        self.now = now
        self.address_book = address_book

    def plan(self, raw, saved, flags, has_attachments=False):
        if not saved.get("checkout_offered") and saved.get("checkout_stage") not in ("details", "submitted", "owner", "olx_waiting"):
            return None
        text = canonical(raw)
        context, sent, guard = copy.deepcopy(saved), {}, {}
        automatic = bool(saved.get('checkout_method') == 'cod' and saved.get('bank_payment') and self.merchant.get('_monobank') and
                         saved['bank_payment'].get('scope') == self.merchant['_monobank'].get('scope'))
        if not saved.get('invoice_issued') and saved.get('cart') and (saved.get('awaiting') or '').startswith('cart_'):
            if self.catalog.explicit_products(text) or requested_sizes(text) or requested_color(text) or requested_quantity(text):
                return None

        def reply(body, human=False):
            if carrier_enabled(self.catalog.shop) and not saved.get('invoice_issued'):
                body = body.replace('для передоплати 100 грн із рештою при отриманні чи для повної оплати товару?',
                                    'для повної оплати товару чи обираєте післяплату з оплатою доставки Новій пошті за ТТН?')
                body = body.replace('післяплатою з передоплатою 100 грн', 'післяплатою з оплатою доставки за ТТН')
                body = body.replace('післяплата з передоплатою 100 грн', 'післяплата з оплатою доставки за ТТН')
            if guard.get("shipping_day"):
                body += "\n\n" + shipping_reply(self.catalog.shop, self.now() if self.now else None)
            if body.count("😊") > 1:
                first = body.index("😊") + 1
                body = body[:first] + body[first:].replace("😊", "").rstrip()
            if "Відправка сьогодні протягом дня" in body or "Постараємося відправити сьогодні" in body:
                guard["calendar_day"] = str(shop_day(self.catalog.shop, self.now() if self.now else None))
            if len(body) > 2000:
                body, human = "Допоможемо узгодити це замовлення особисто, щоб правильно оформити всі речі й дані відправки.", True
                sent["invoice_issued"] = False
            if human:
                sent["handoff_now"] = True
                sent["checkout_stage"] = "owner"
            return {"text": body, "context": context, "on_send": sent, "guard": guard, "notes": []}

        if "complaint" in flags:
            return reply("Зараз допоможу вам особисто й перевірю деталі замовлення.", True)
        if flags and flags <= {"thanks"}:
            sent["awaiting"] = saved.get("awaiting")
            return reply("Звертайтесь")
        if direct_delivery(saved) and saved.get('invoice_issued'):
            from carrier_checkout import plan as carrier_plan
            return carrier_plan(self, raw, saved, flags, has_attachments)
        if saved.get("invoice_issued") and (asks_size_advice(text, saved.get("awaiting")) or saved.get("checkout_size_advice")):
            items = order_items(self.catalog, context)
            keys = {item["product"] for item in items}
            named = self.catalog.explicit_products(text)
            selected = named[0]["key"] if len(named) == 1 and named[0]["key"] in keys else context.get("checkout_advice_product")
            selected = selected or (next(iter(keys)) if len(keys) == 1 else None)
            context["checkout_size_advice"] = True
            if not selected:
                return reply("Для якої речі підібрати розмір? Напишіть назву, будь ласка — решту замовлення збережу.")
            context["checkout_advice_product"] = selected
            product = self.catalog.products.get(selected, {})
            step = height_advice(product, text, context)
            if not step:
                return reply("Для цієї моделі уточнимо підбір розміру особисто, щоб правильно оформити замовлення.", True)
            if step.get("question"):
                sent["awaiting"] = step.get("awaiting")
                return reply(step["question"], step.get("human", False))
            if step.get('candidates'):
                try:
                    rows=stock_rows(self.inventory,product)
                except StockUnavailable:
                    return reply('Звірю наявність відповідних розмірів перед зміною. Ваше замовлення збережено.')
                item=next(item for item in items if item['product']==selected)
                step['size']=preferred_available_size(rows,step['candidates'],item.get('color'),item.get('quantity',1),step.get('size'))
                guard['stocks']={selected:stock_signature(rows)}
            context.pop("checkout_size_advice", None)
            context.pop("checkout_advice_product", None)
            sent["awaiting"] = "shipping_details"
            if any(item["size"] != step["size"] for item in items if item["product"] == selected):
                context.pop("customer_confirmation", None)
                return reply("Вам підійде %s. У замовленні зараз інший розмір — власник допоможе узгодити зміну перед відправкою. Повторно нічого не оплачуйте." % step["size"], True)
            body = "Вам підійде %s — у замовленні вже обраний саме цей розмір." % step["size"]
            details = context.get("shipping_details", {})
            if any(not details.get(key) for key in LABELS):
                body += "\n\n" + missing_details_prompt(details)
            return reply(body)
        verified = saved.get("payment_verified") and saved.get("verified_invoice_key") == invoice_key(snapshot(self.catalog, saved))
        if verified and saved.get("customer_confirmation") == confirmation_key(snapshot(self.catalog, saved)):
            if "dispatch" in flags or flags and flags <= {"thanks", "greeting"}:
                return None
            if "cancel" in flags or re.search(r"змін|замін|прибери|додай|добав|передум|телефон|місто|город|село|відділен|отделен|поштомат|почтомат", text):
                context.pop("customer_confirmation", None)
                return reply("Передам зміни власнику, щоб правильно узгодити замовлення перед відправкою. Повторно нічого не оплачуйте.", True)
            if confirms(text):
                return reply("Замовлення записав, дякую! Якщо потрібно щось уточнити — пишіть.")
            return None
        if not verified and saved.get("checkout_stage") in ("submitted", "owner") and saved.get("customer_confirmation"):
            if requested_quantity(text) is not None and not has_attachments:
                if saved.get('parcel', {}).get('ttn'):
                    return reply('Передам зміну кількості власнику: накладну вже створено. Повторно нічого не оплачуйте.', True)
                context.update(checkout_stage='details', customer_confirmation=None, confirmation_sent=None)
                return self.plan(raw, context, flags, has_attachments)
            if not has_attachments and re.search(r'телефон|номер\s+тел|відділен|отделен|поштомат|почтомат|місто\s*[:—]|город\s*[:—]', text):
                changed = shipping_details(raw, saved.get('shipping_details', {}))
                if changed != saved.get('shipping_details', {}):
                    if saved.get('parcel', {}).get('ttn'):
                        return reply('Передам виправлення власнику: накладну вже створено, тому адресу потрібно змінити й у ній. Повторно нічого не оплачуйте.', True)
                    context.update(shipping_details=changed, checkout_stage='details', customer_confirmation=None, confirmation_sent=None)
                    return self.plan(raw, context, flags, has_attachments)
            if flags and flags <= {"thanks", "greeting"}:
                return None
            if "cancel" in flags or re.search(r"змін|замін|прибери|додай|добав|передум", text):
                return reply("Допоможемо узгодити зміни до оформленого замовлення. Повторно нічого не оплачуйте.", True)
            if has_attachments or reports_payment(text) or declares_receipt(text):
                denied = bool(re.search(r'не\s+(?:квитанц|квитанці|чек)|фото\s+(?:товару|речі)|випадков|случайно', text))
                receipt_image = has_attachments and not denied and (saved.get("receipt_expected") or saved.get("awaiting") == "payment_receipt" or declares_receipt(text) or reports_payment(text) or saved.get('parcel', {}).get('payment_review_revision'))
                context["receipt_submitted"] = bool(receipt_image or context.get("receipt_submitted"))
                if receipt_image or reports_payment(text):
                    context["payment_reported"] = True
                sent.update(checkout_stage="submitted", handoff_now=False, awaiting=None)
                if has_attachments and not receipt_image:
                    context['unclassified_attachment'] = not denied
                    return reply('Фото отримав. Підкажіть, що саме хочете показати або уточнити щодо товару?' if not denied else 'Зрозуміло, це фото не позначаю як квитанцію. Що саме хочете уточнити?')
                if context.get("receipt_submitted"):
                    sent["dispatch_announced_key"] = invoice_key(snapshot(self.catalog, context))
                    return reply("Дякую, квитанцію отримав! " + shipping_reply(self.catalog.shop, self.now() if self.now else None))
                return reply("Дякую! Надішліть, будь ласка, фото або скріншот квитанції в цей чат. Повторно оплачувати не потрібно.")
            if confirms(text):
                return reply("Замовлення записав, дякую!")
            return None
        if "cancel" in flags:
            if saved.get("receipt_submitted") or saved.get("payment_reported") or saved.get("unclassified_attachment"):
                return reply("Зрозуміло. Спочатку перевіримо оплату й особисто узгодимо скасування замовлення.", True)
            for key in ("checkout_offered", "checkout_stage", "checkout_method", "checkout_amount", "pending_checkout_method", "shipping_details", "shipping_awaiting", "shipping_attempts", "receipt_submitted", "order_requested", "cart_order_requested", "cart", "size", "color", "quantity", "awaiting", "invoice_id", "invoice_items", "invoice_total", "invoice_issued", "invoice_payment_signature", "customer_confirmation", "confirmation_sent", "quantity_change", "unclassified_attachment", "payment_reported", "confirmation_attempts"):
                context.pop(key, None)
            return reply("Добре, оформлення скасовано. Якщо захочете повернутися до вибору — пишіть.")
        if saved.get("checkout_stage") == "details":
            context = restore_legacy_invoice(self.catalog, context)
            change = quantity_change(self.catalog, self.inventory, text, context, has_attachments)
            if change is not None:
                body, human, changes_guard = change
                if saved.get("parcel", {}).get("ttn"):
                    human = True
                    body += "\n\nНакладну вже підготовлено. Власник звірить зміни перед відправкою; повторно нічого не переказуйте."
                guard.update(changes_guard)
                if saved.get("checkout_method") == "full" and context.get("invoice_items") != saved.get("invoice_items"):
                    if not self.merchant or saved.get("invoice_payment_signature") != payment_signature(self.merchant):
                        context = copy.deepcopy(saved)
                        return reply("Перед зміною суми звіримо актуальні реквізити саме для цього замовлення. Поки нічого не переказуйте.", True)
                    guard["payment_settings"] = payment_signature(self.merchant)
                if not human and not context.get("quantity_change"):
                    details = context.get("shipping_details", {})
                    if any(not details.get(key) for key in LABELS):
                        body += "\n\n" + missing_details_prompt(details)
                    else:
                        review = snapshot(self.catalog, context)
                        sent.update(confirmation_sent=confirmation_key(review), awaiting="order_confirmation")
                        body += "\n\nДані відправки збережено:\n" + "\n".join(LABELS[key] + ": " + details[key] for key in LABELS) + "\nУсе правильно?"
                return reply(body, human)
            if flags.intersection({"dispatch", "arrival"}) and not has_attachments:
                return None
            repeat = requests_invoice(text) and not has_attachments
            if repeat:
                if verified:
                    details = context.get("shipping_details", {})
                    body = "Повторний переказ не потрібний."
                    if any(not details.get(key) for key in LABELS):
                        body += "\n\n" + missing_details_prompt(details)
                    else:
                        body += " " + shipping_reply(self.catalog.shop, self.now() if self.now else None)
                    return reply(body)
                if saved.get("receipt_submitted"):
                    return reply("Квитанцію вже отримано. Повторно переказувати кошти не потрібно — перевіримо надходження.", True)
                if (not self.merchant or saved.get("invoice_payment_signature") != payment_signature(self.merchant)):
                    return reply("Уточнимо актуальні реквізити саме для цього замовлення перед переказом.", True)
                guard["payment_settings"] = payment_signature(self.merchant)
                return reply("Ось реквізити для цього ж замовлення:\n\n" + payment_details(self.merchant, saved["checkout_amount"], saved.get('checkout_method') == 'full') +
                             ("\n\nЯкщо вже переказали кошти, повторно платити не потрібно. Надішліть квитанцію в цей чат; надходження звіримо через mono." if automatic else
                              "\n\nЯкщо вже переказали кошти, ще раз платити не потрібно — надішліть квитанцію."))
            shipping_edit = bool(re.search(r"\b(?:телефон\w*|номер\s+тел\w*|піб|фіо|фио|отримувач\w*|місто|город|село|адрес\w*|відділен\w*|отделен\w*|поштомат\w*|почтомат\w*)\b", text))
            explicit = self.catalog.explicit_products(text)
            if not shipping_edit and ("measurements" in flags or context.get("checkout_faq") == "measurements" and len(explicit) == 1):
                keys = {item["product"] for item in order_items(self.catalog, context)}
                product = explicit[0] if len(explicit) == 1 else self.catalog.products.get(next(iter(keys))) if len(keys) == 1 else None
                if not product:
                    context["checkout_faq"] = "measurements"
                    return reply("Про яку річ показати заміри? Напишіть назву товару, будь ласка.")
                context.pop("checkout_faq", None)
                sizes = requested_sizes(text) or [item["size"] for item in order_items(self.catalog, context) if item["product"] == product["key"] and item.get("size")]
                body, missing_measurements = measurements_reply(product, sizes or None)
                return reply("Ось заміри в сантиметрах:\n" + body if body else "Точні заміри цього товару уточнимо для вас.", missing_measurements)
            if not shipping_edit and "price" in flags:
                order = snapshot(self.catalog, context)
                if order["total"] is None:
                    return reply("Уточнимо повну суму замовлення перед оплатою.", True)
                return reply("Разом за вибрані товари — %s грн. %s" % (order["total"],
                             "Передоплата — 100 грн, решта за товар при отриманні." if order["method"] == "cod" else "Це повна оплата товарів; при отриманні оплачується лише доставка."))
            if not shipping_edit and "availability" in flags and "order" not in flags:
                items = order_items(self.catalog, context)
                keys = {item["product"] for item in items}
                product = explicit[0] if len(explicit) == 1 else self.catalog.products.get(next(iter(keys))) if len(keys) == 1 else None
                if not product:
                    return reply("Для якої речі перевірити наявність? Напишіть назву, розмір і колір. Поточне замовлення збережено.")
                matching_items = [item for item in items if item["product"] == product["key"]]
                item = matching_items[0] if len(matching_items) == 1 else {}
                sizes, color = requested_sizes(text) or [item.get("size")], requested_color(text) or item.get("color")
                amount = requested_quantity(text) or item.get("quantity", 1)
                try:
                    rows = stock_rows(self.inventory, product)
                except StockUnavailable:
                    return reply("Уточнимо актуальну наявність особисто. Ваше оформлене замовлення збережено.", True)
                guard["stocks"] = {product["key"]: stock_signature(rows)}
                available = any(r["available"] >= amount and (sizes == [None] or r["size_code"] in sizes) and (not color or r["color_code"] == color) for r in rows)
                return reply(("Так, цей варіант є в наявності." if available else "Цього варіанта в потрібній кількості зараз немає.") + " Склад вашого замовлення поки залишаю без змін.")
            method_again = method_choice(text) if len([line for line in raw.splitlines() if line.strip()]) == 1 and re.search(r"[а-яіїєґa-z]{3}", text) else None
            if method_again == saved.get("checkout_method") and not shipping_edit:
                return reply("Так, спосіб оформлення вже записано. Реквізити й сума є вище. " +
                             ("Надішліть, будь ласка, квитанцію в цей чат. Надходження також перевіримо через mono." if automatic else
                              "Очікую квитанцію про оплату." if not saved.get("receipt_submitted") else "Квитанцію отримано, залишилося звірити дані відправки."))
            if (re.search(r"замін|замість|вместо|додай|добав|прибери|убери", text) and
                    (not shipping_edit or self.catalog.explicit_products(text))):
                return reply("Можемо змінити замовлення. Узгодимо новий склад і суму перед оплатою, щоб нічого не переплутати.", True)
            context.pop("checkout_faq", None)
            # An image is opaque to this rules-based assistant. Its purpose comes from
            # the customer's caption/answer or an explicit request for a receipt.
            purpose_reply = bool(saved.get("unclassified_attachment") and
                                 (declares_receipt(text) or
                                  saved.get("awaiting") == "attachment_purpose" and confirms(text)))
            receipt_attachment = has_attachments and not re.search(r"не\s+(?:квитанц|квитанці|чек)|фото\s+(?:товару|речі)|випадков|случайно", text) and (saved.get("awaiting") == "payment_receipt" or declares_receipt(text) or reports_payment(text))
            if reports_payment(text):
                context["payment_reported"] = True
            if declares_receipt(text):
                context["payment_reported"] = True
            if purpose_reply or receipt_attachment:
                context["receipt_submitted"] = True
                context.pop("unclassified_attachment", None)
            elif has_attachments:
                # The owner reviews attachments; never ask the buyer to send payment twice.
                context["unclassified_attachment"] = True
            elif saved.get("unclassified_attachment") and re.search(r"не\s+(?:квитанц|квитанці|чек)|фото\s+(?:товару|речі|задач)|випадков|случайно", text):
                context.pop("unclassified_attachment", None)
                sent["awaiting"] = "shipping_details"
                return reply("Зрозуміло, це фото не позначаю як квитанцію.\n\n" + missing_details_prompt(context.get("shipping_details", {})))
            if saved.get("awaiting") == "attachment_purpose":
                if not purpose_reply and not receipt_attachment:
                    details = shipping_details(raw, saved.get("shipping_details", {}), saved.get("shipping_awaiting"))
                    if details == saved.get("shipping_details", {}):
                        sent["awaiting"] = "attachment_purpose"
                        return reply("Підкажіть, будь ласка, це квитанція про оплату чи інше фото? Якщо квитанція — достатньо написати «так».")
                    # Delivery data can arrive while an unrelated image is unresolved.
                    # Continue collecting them without treating the image as a receipt.
                sent["awaiting"] = "shipping_details"
            shipping_raw = raw
            details = shipping_details(shipping_raw, saved.get("shipping_details", {}), saved.get("shipping_awaiting"))
            receipt = bool(context.get("receipt_submitted") or re.search(r"https://\S+", text) and declares_receipt(text))
            context["shipping_details"] = details
            context["receipt_submitted"] = receipt
            if has_attachments and context.get('unclassified_attachment') and not receipt_attachment:
                sent['awaiting'] = 'attachment_purpose'
                return reply('Фото отримав. Це квитанція про оплату чи фото товару? Дані замовлення збережені.')
            missing = [label for key, label in LABELS.items() if not details.get(key)]
            if re.search(r"(?:скрін|скрин|фото).{0,20}(?:квитанц|чек)|(?:квитанц|чек).{0,20}(?:скрін|скрин|фото)", text) and not has_attachments:
                if automatic:
                    return reply("Надішліть, будь ласка, фото або скріншот квитанції в цей чат. Надходження також звіримо через mono; повторно оплачувати не потрібно.")
                return reply("Так, можна надіслати фото або скріншот квитанції сюди 😊")
            if missing:
                context["shipping_awaiting"] = next(key for key in LABELS if not details.get(key))
                bulk_request = bool(re.search(r"разом|вместе|ус[еі]\s+дан|вс[еіёьо]+\s+(?:разом|сразу)|одним\s+повідом", text))
                attempts = 0 if details != saved.get("shipping_details", {}) or bulk_request or purpose_reply else saved.get("shipping_attempts", 0) + (0 if has_attachments else 1)
                context["shipping_attempts"] = attempts
                if attempts>=4:
                    return reply('Можемо повернутися до оформлення, коли буде зручно. Надішліть решту даних — продовжимо з цього місця.')
                if attempts >= 2:
                    from conversation_policy import next_step
                    return reply(next_step(self.catalog,context))
                intro = "Дякую, вкладення для перевірки оплати отримав.\n\n" if receipt_attachment or purpose_reply else ""
                return reply(intro + missing_details_prompt(details))
            context.pop("shipping_awaiting", None)
            context.pop("shipping_attempts", None)
            if self.address_book:
                from shipping_directory import details_key
                if context.get("shipping_directory_key") != details_key(details) or not details.get("warehouse_ref"):
                    review = self.address_book.review(details, saved.get("shipping_validation"), text)
                    if review.get("details"):
                        details = review["details"]
                        context["shipping_details"] = details
                    if review["status"] != "ready":
                        context["shipping_validation"] = review
                        context.pop("shipping_directory_key", None)
                        context.pop("customer_confirmation", None)
                        context.pop("confirmation_sent", None)
                        sent["awaiting"] = "shipping_address"
                        return reply(review["prompt"], review["status"] == "unavailable")
                    details = review["details"]
                    context["shipping_details"] = details
                    context["shipping_directory_key"] = review["key"]
                    context.pop("shipping_validation", None)
            if saved.get("parcel", {}).get("ttn") and details != saved.get("shipping_details", {}):
                context.pop("customer_confirmation", None)
                return reply("Записав оновлені дані:\n\n" + summary(snapshot(self.catalog, context), reference=False) +
                             "\n\nНакладну вже підготовлено, тому власник перевірить зміну адреси перед відправкою. Повторно оплачувати не потрібно.", True)
            order = snapshot(self.catalog, context)
            check = confirmation_key(order)
            if (not purpose_reply and not has_attachments and saved.get("confirmation_sent") == check and confirms(text) and details == saved.get("shipping_details", {})):
                context["customer_confirmation"] = check
            if context.get("customer_confirmation") != check:
                context.pop("customer_confirmation", None)
                sent.update(confirmation_sent=check, awaiting="order_confirmation")
                if re.fullmatch(r"(?:ні|нет|неправильно|невірно)[.! ]*", text):
                    context["confirmation_attempts"] = saved.get("confirmation_attempts", 0) + 1
                    if context["confirmation_attempts"] >= 2:
                        return reply("Допоможемо виправити дані особисто. Передам ваше замовлення власнику.", True)
                    return reply("Що виправити? Напишіть, наприклад, «Місто: Київ, Київська область» або «Поштомат №12345». Можна змінити лише потрібний рядок — решту даних збережу.")
                same_review = saved.get("confirmation_sent") == check
                context["confirmation_attempts"] = saved.get("confirmation_attempts", 0) + 1 if same_review and not has_attachments else 0
                if context["confirmation_attempts"] >= 2:
                    return reply("Допоможемо звірити замовлення й дані відправки особисто, щоб усе було правильно.", True)
                if same_review:
                    return reply("Вкладення отримав. Дані замовлення й адреса — вище. Усе правильно?" if has_attachments else
                                 "Підкажіть, дані для відправки правильні чи потрібно щось змінити?")
                intro = "Звірив адресу з довідником Нової пошти. Перевірте, будь ласка, замовлення та повну адресу:\n\n" if self.address_book else "Перевірте, будь ласка, замовлення й дані відправки:\n\n"
                return reply(intro + summary(order, reference=False) +
                             "\n\nУсе правильно? Напишіть «так» або що потрібно виправити.")
            context.pop("confirmation_attempts", None)
            # The dispatch plan is customer-facing; it is not proof of payment.
            # Owner/bank decisions still control the actual shipment in Ledgerly.
            sent.update(checkout_stage="submitted", handoff_now=False, awaiting=None)
            if context.get('receipt_required') and not context.get('receipt_submitted'):
                sent['awaiting'] = 'payment_receipt'
            sent["dispatch_announced_key"] = invoice_key(snapshot(self.catalog, context))
            return reply("Дякую! " + shipping_reply(self.catalog.shop, self.now() if self.now else None))
        if "arrival" in flags or "dispatch" in flags and re.match(r"(?:коли|когда)\b", text):
            return None
        choice = method_choice(text)
        if choice in ("cod", "olx", "full") and re.match(r"(?:як\b|как\b|чим\b|чому\b|почему\b|скільки\b|сколько\b)", text):
            sent["pending_checkout_method"] = choice
            explanation = {"cod": "Для післяплати доставку оплачуєте Новій пошті за ТТН, а повну вартість товару — при отриманні." if carrier_enabled(self.catalog.shop) else "Для післяплати передоплата — 100 грн, решта вартості товару при отриманні.",
                           "olx": "OLX Доставка оформлюється й оплачується в самому OLX.",
                           "full": "При повній оплаті товар оплачуєте за реквізитами, а при отриманні — лише доставку."}[choice]
            return reply(explanation + " Бажаєте обрати цей спосіб?")
        if choice is None and saved.get("pending_checkout_method") and re.fullmatch(r"(?:так|да|добре|ок|давайте)[.! ]*", text):
            choice = saved["pending_checkout_method"]
        if choice is None:
            if requests_invoice(text):
                return reply("Звісно. Реквізити надіслати для передоплати 100 грн із рештою при отриманні чи для повної оплати товару? Також можна оформити OLX Доставку в застосунку.")
            if re.fullmatch(r"(?:так|да|добре|ок|окей|давайте)[.! ]*", text):
                return reply("Який варіант обираєте: OLX Доставка, післяплата з передоплатою 100 грн чи повна оплата? 😊")
            if has_attachments and not reports_payment(text):
                if declares_receipt(text):
                    context["receipt_submitted"] = True
                    return reply("Вкладення для перевірки оплати отримав. Уточнимо оформлення й надходження коштів особисто.", True)
                context["unclassified_attachment"] = True
                return reply("Фото отримав. Що саме хочете показати або уточнити щодо нього?")
            if reports_payment(text):
                return reply("Дякую 😊 Перевірю деталі оплати та оформлення особисто.", True)
            if (asks_size_advice(text, saved.get("awaiting")) or flags.intersection({"sizing", "price", "availability", "measurements", "photos", "authenticity"}) or requested_sizes(text) or requested_color(text)):
                return None
            return reply("Оформлюємо OLX Доставкою, післяплатою з передоплатою 100 грн чи з повною оплатою? Можна просто написати назву або номер варіанта.")
        if choice == "ambiguous":
            return reply("Який спосіб обираєте: OLX Доставка, післяплата з передоплатою 100 грн чи повна оплата товару? 😊")
        if choice in ('cod', 'full') and (saved.get('olx_notice_received') or saved.get('olx_delivery_reported')):
            return reply('Спочатку звіримо вже оформлене замовлення в OLX, щоб не створити друге й не попросити оплату двічі. Якщо хочете змінити спосіб доставки, узгодимо це з власником.', True)
        if choice == "cod" and cod_deposit_question(text, saved) == 'without' and not carrier_enabled(self.catalog.shop):
            return reply(COD_WITHOUT_PREPAYMENT)
        if "dispatch" in flags:
            guard["shipping_day"] = str(shop_day(self.catalog.shop, self.now() if self.now else None))
        sent["pending_checkout_method"] = None
        cart = saved.get("cart") or [{"product": saved.get("product"), "size": saved.get("size"), "color": saved.get("color"), "quantity": saved.get("quantity", 1)}]
        if any(not item.get("size") or not item.get("color") or item.get("product") not in self.catalog.products for item in cart):
            return reply("Спочатку уточню всі вибрані розміри й кольори, щоб оформити замовлення правильно 😊", True)
        demands = Counter()
        for item in cart:
            demands[item["product"], item["size"], item["color"]] += item["quantity"]
        stocks, signatures = {}, {}
        try:
            for key, size, color in demands:
                if key not in stocks:
                    rows = stock_rows(self.inventory, self.catalog.products[key])
                    stocks[key], signatures[key] = stock_groups(rows), stock_signature(rows)
                if stocks[key].get((size, color), 0) < demands[key, size, color]:
                    return reply("Поки оформляли замовлення, наявність змінилася. Уточню варіанти для вас перед оплатою 😊", True)
        except StockUnavailable:
            return reply("Перед оплатою потрібно ще раз уточнити наявність. Зараз допоможу вам особисто 😊", True)
        guard["stocks"] = signatures
        sent.update(checkout_method=choice)
        if choice == "olx":
            sent['invoice_id'] = context.get('invoice_id') or new_order_number()
            if sum(item["quantity"] for item in cart) > 1:
                return reply("Добре, оформимо через OLX Доставку 😊 У вас кілька речей — узгоджу оформлення всього замовлення, щоб сума була правильною.", True)
            sent['checkout_stage'] = 'olx_waiting'
            carrier = ' Оберіть Укрпошту під час оформлення.' if context.get('olx_carrier') == 'ukrposhta' else ''
            return reply("Добре, очікую оформлення OLX Доставки в цьому оголошенні. Оплата проходить у самому OLX." + carrier + " Якщо виникне запитання — пишіть, допоможу!\nВаше замовлення №" + sent['invoice_id'])
        if choice == 'cod' and carrier_enabled(self.catalog.shop):
            guard.pop('shipping_day', None)
            prices = [self.catalog.products[item['product']].get('price_uah') for item in cart]
            if any(price is None for price in prices):
                return reply('Уточню точну ціну вибраних речей перед оформленням.', True)
            current_selection = {key: value for key, value in saved.items() if key != 'invoice_items'}
            sent.update(checkout_stage='details', checkout_amount='0', checkout_delivery_prepay=True,
                        awaiting='shipping_details', invoice_id=new_order_number(),
                        invoice_items=order_items(self.catalog, current_selection),
                        invoice_total=money(total_price(prices, cart)), invoice_issued=True,
                        customer_confirmation=None, confirmation_sent=None, payment_verified=False,
                        verified_invoice_key=None, shipping_directory_required=bool(self.address_book))
            order = snapshot(self.catalog, dict(context, **sent))
            return reply('Добре, оформимо післяплатою. За товар — %s грн при отриманні. Доставку оплачуєте безпосередньо Новій пошті за ТТН.\n\n' % order['total'] +
                         summary(order, shipping=False, payment=False) + '\n\nСпочатку підготуємо накладну. Для цього надішліть одним повідомленням:\n' + DETAILS +
                         '\n\nДля села додайте район. Я звірю адресу й покажу її перед оформленням.')
        if not self.merchant:
            return reply("Уточню реквізити й надішлю їх вам для оплати 😊", True)
        prices = [self.catalog.products[item["product"]].get("price_uah") for item in cart]
        if any(price is None for price in prices):
            return reply("Перед оплатою уточню точну вартість усіх товарів і надішлю вам реквізити.", True)
        amount = 100 if choice == "cod" else total_price(prices, cart)
        if choice == "cod" and total_price(prices, cart) < amount:
            return reply("Для цього замовлення уточнимо суму передоплати особисто, щоб ви не переказали більше вартості товарів.", True)
        intro = ("Добре, оформимо післяплатою. Передоплата — 100 грн, %s грн за товар — при отриманні." % money(total_price(prices, cart) - amount)
                 if choice == "cod" else "Добре, оформимо з повною оплатою товару. При отриманні сплачуєте лише доставку.")
        current_selection = {key: value for key, value in saved.items() if key != "invoice_items"}
        sent.update(checkout_stage="details", checkout_amount=money(amount), awaiting="shipping_details", invoice_id=new_order_number(),
                    invoice_items=order_items(self.catalog, current_selection), invoice_total=money(total_price(prices, cart)), invoice_issued=True,
                    invoice_payment_signature=payment_signature(self.merchant), customer_confirmation=None, confirmation_sent=None,
                    payment_verified=False, verified_invoice_key=None, confirmation_attempts=0,
                    receipt_required=True, receipt_expected=True)
        sent["shipping_directory_required"] = bool(self.address_book)
        guard["payment_settings"] = payment_signature(self.merchant)
        order = snapshot(self.catalog, dict(context, **sent))
        payment_hint = ("Після оплати надішліть, будь ласка, фото або скріншот квитанції в цей чат. " +
                        ("Надходження також перевіримо автоматично через mono. " if choice == 'cod' and self.merchant.get('_monobank') else "") + "\n\n")
        return reply(intro + "\n\n" + summary(order, shipping=False, reference=False, payment=False) + "\n\n" + payment_details(self.merchant, money(amount), choice == 'full') + "\n\n" + payment_hint +
                     "Для відправки заповніть одним повідомленням:\n" + DETAILS +
                     "\n\nДля села додайте район. Адресу ще покажу вам для перевірки.")
