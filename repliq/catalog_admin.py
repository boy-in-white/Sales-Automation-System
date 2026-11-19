"""Serialized private-chat admin work; OLX reads and local Repliq bindings only."""

import math
import re
import time

from app import AppError
from inventory import StockUnavailable
from product_registry import ranked_products, words


def admin_owner(config):
    if not config:
        return None
    owners = config.get("owner_ids", [])
    return config.get("admin_owner_id") or (owners[0] if len(owners) == 1 else None)


def advert_price(advert):
    price = advert.get("price")
    if not isinstance(price, dict) or price.get("currency") != "UAH" or type(price.get("value")) is bool:
        return None
    try:
        value = float(price["value"])
        return value if math.isfinite(value) and 0 < value <= 1000000 else None
    except (KeyError, ValueError, TypeError):
        return None


def process_admin(worker, allowed=None):
    registry = worker.registry
    job = registry.next_job(allowed=allowed, claim=True)
    if not job:
        return
    owner, key = job["owner"], job["event_key"]
    if owner != admin_owner(worker.telegram_config) or time.time() - job["created"] > 900:
        registry.finish(key, "rejected")
        return
    try:
        payload, action = job["payload"], job["action"]
        argument = payload.get("argument", "").strip()
        if action.startswith('sizing_'):
            from product_sizing import process
            process(worker,job)
        elif action == "refresh":
            worker.refresh_products(force=True)
            registry.tell(owner, "result:" + key, "Каталог із Ledgerly оновлено: %s товарів. Залишки не змінювалися." % len(registry.products()))
        elif action == "products":
            products = registry.products()
            if not products:
                worker.refresh_products(force=True)
                products = registry.products()
            query = words(argument)
            found = [p for p in products if not query or query in words(p["canonical_name"] + " " + " ".join(p.get("aliases", [])))]
            names = ["• " + p["canonical_name"][:120] + (" · " + "/".join(sorted(set(p.get("available_sizes", [])))) if p.get("available_sizes") else "") for p in found[:20]]
            buttons = [[{'text':'✏️ '+p['canonical_name'][:55], 'callback_data':registry.choice(owner,{'_action':'sizing_open','product_id':str(p['id'])})}]
                       for p in found[:20]]
            registry.tell(owner, "result:" + key, "📦 Товари Ledgerly · %s\n%s\n\nОбери товар нижче, щоб змінити заміри, сітку або характеристики. Для пошуку надішли назву. Ціни й залишки ведуться в Ledgerly." % (len(found), "\n".join(names) or "Нічого не знайдено."),
                          {'inline_keyboard':buttons} if buttons else None)
        elif action == "ads":
            names = {str(p["id"]): p["canonical_name"] for p in registry.products()}
            rows = {b["advert"]: b for b in registry.bindings()}
            for ad, product_key in worker.catalog.adverts.items():
                rows.setdefault(ad, {"advert": ad, "product_id": worker.catalog.products[product_key].get("ledgerly_product_id"), "active": 1})
            rows = [b for b in rows.values() if not argument or argument in b["advert"]]
            lines = ["%s → %s · %s" % (b["advert"], names.get(b["product_id"], "товар недоступний у Ledgerly"),
                     "увімкнено" if b["advert"] in worker.catalog.adverts else "вимкнено / товар неактивний") for b in rows[:20]]
            keyboard = [([{"text": "✏️ Змінити №" + b["advert"], "callback_data": "catalog:edit_ad:" + b["advert"]},
                          {"text": "⏸ Вимкнути", "callback_data": "catalog:remove_ad:" + b["advert"]}] if b["advert"] in worker.catalog.adverts else
                         [{"text": "▶️ Увімкнути №" + b["advert"], "callback_data": "catalog:edit_ad:" + b["advert"]}]) for b in rows[:8]]
            registry.tell(owner, "result:" + key, "📋 Оголошення Repliq\n\n" + ("\n".join(lines) or "Ще немає. Натисни «⚙️ Керування каталогом» → «➕ Додати оголошення».") +
                          "\n\nНатисни потрібне оголошення нижче, щоб змінити прив'язку або призупинити відповіді.", {"inline_keyboard": keyboard} if keyboard else None)
        elif action == "remove_ad":
            if not re.fullmatch(r"[0-9]{1,15}", argument) or int(argument) == 0:
                raise AppError("Надішли лише числовий ID оголошення OLX, наприклад 123456789.")
            argument = str(int(argument))
            registry.disable(argument)
            worker.compose_products()
            worker.invalidate_ad(argument)
            registry.tell(owner, "result:" + key, "Оголошення %s вимкнено в Repliq. Оголошення OLX і дані Ledgerly залишаються на місці." % argument)
        elif action in ("add_ad", "edit_ad", "bind"):
            worker.refresh_products(force=True)
            products = registry.products()
            if not products:
                raise AppError("Активних товарів у Ledgerly ще немає. Додай їх там, а потім обери «⚙️ Керування каталогом» → «🔄 Оновити каталог».")
            if action == "bind":
                ad, search = str(payload["advert"]), ""
            else:
                parts = argument.split(maxsplit=1)
                ad, search = (parts[0], parts[1] if len(parts) > 1 else "") if parts else ("", "")
            if not re.fullmatch(r"[0-9]{1,15}", ad) or int(ad) == 0:
                raise AppError("Надішли числовий ID оголошення. Можна додати назву товару після нього: 123456789 футболка.")
            ad = str(int(ad))
            if str(worker.client.identity()["id"]) != worker.account:
                raise AppError("Акаунт OLX змінився. Потрібен новий запуск Repliq.")
            advert = worker.client.advert(ad)
            selected = None
            if action == "bind":
                selected = next((p for p in products if str(p["id"]) == str(payload["product_id"])), None)
                if not selected:
                    raise AppError("Вибраний товар уже неактивний або видалений у Ledgerly. Онови каталог кнопкою в меню та вибери знову.")
            elif search:
                exact = [p for p in products if words(p["canonical_name"]) == words(search)]
                found = exact or [p for p in products if words(search) in words(p["canonical_name"] + " " + " ".join(p.get("aliases", [])))]
                if len(found) == 1:
                    selected = found[0]
            else:
                ranked = ranked_products(products, advert["title"], registry.bindings())
                if action == "add_ad" and ranked and ranked[0][0] >= 90 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
                    selected = ranked[0][1]
                found = [p for _, p in ranked] or products
            if selected:
                # A fresh baseline prevents enabling an advert from answering its backlog.
                worker.baseline_ad(ad)
                registry.bind(ad, str(selected["id"]), advert["title"], advert_price(advert))
                worker.compose_products()
                worker.invalidate_ad(ad)
                registry.tell(owner, "result:" + key, "Збережено: %s → %s. Оброблятимуться лише нові повідомлення.\n"
                              "Прив'язка діє відразу; перезапуск не потрібний." % (ad, selected["canonical_name"]),
                              {'inline_keyboard': [[{'text':'📏 Налаштувати товар', 'callback_data':registry.choice(owner, {'_action':'sizing_open','product_id':str(selected['id'])})}]]})
            else:
                if not found:
                    raise AppError("Не знайшов відповідний товар у Ledgerly. Відкрий «📦 Товари», знайди потрібну назву й повтори додавання оголошення.")
                choices = found[:8]
                keyboard = [[{"text": p["canonical_name"][:60], "callback_data": registry.choice(owner, {"advert": ad, "product_id": str(p["id"])})}] for p in choices]
                registry.tell(owner, "result:" + key, "Оголошення %s: %s\nВибери відповідний товар Ledgerly.\n"
                              "Якщо потрібної речі тут немає, перевір її в меню «📦 Товари»." % (ad, advert["title"][:300]), {"inline_keyboard": keyboard})
        else:
            raise AppError("Невідома команда керування Repliq.")
        registry.finish(key)
    except (AppError, StockUnavailable) as error:
        from product_sizing import session_keyboard
        registry.tell(owner, "error:" + key, str(error) + "\nЗміни в Ledgerly не виконувалися.",
                      session_keyboard(back=True) if job['action']=='sizing_preview' else None)
        registry.finish(key, "failed")
