"""Private-chat commands and forwarding into the employees topic."""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from auth import ANALYTICS_ACCESS
from backup import create_backup
from business_db import (
    add_product_alias,
    attach_submission_message,
    business_health,
    create_submission,
    current_month_label,
    edit_order_by_ttn,
    export_1c_csv,
    format_money,
    get_monthly_turnover,
    get_sales_stats,
    list_products,
    rename_product,
    resolve_product,
)
from config import (
    DATABASE_URL,
    TG_EMPLOYEES_THREAD_ID,
    TG_GROUP_ID,
    TIMEZONE,
    is_admin,
    is_owner,
    is_private_user_allowed,
)
from db_runtime import database_backend
from inventory import (
    handle_inventory_text,
    send_inventory_menu,
    start_inventory_product_creation,
)
from parser import extract_ttns, parse_message
from tracking_db import (
    database_health,
    export_shipments_csv,
    get_shipment,
    reactivate_shipment,
)
from tracking_notify import build_employee_text
from tracking_service import fetch_tracking_snapshot, refresh_ttn
from ui import clear_current_month_keyboard, submission_keyboard
from validator import validate

LOGGER = logging.getLogger(__name__)
PROTECTED_DATA_COMMANDS = {
    "/turnover",
    "/oborot",
    "/stats",
    "/export",
    "/export1c",
    "/clear_month",
}


def private_help_text(admin: bool = False, owner: bool = False) -> str:
    lines = [
        "Привіт. Я Ledgerly — твій спокійний помічник з обліку. 🤍",
        "",
        "Нове замовлення надішли одним повідомленням:",
        "",
        "Імʼя клієнта",
        "Опис товару",
        "Ціна: 1090",
        "Собівартість: 500 (необовʼязково)",
        "Доставка: 100 (необовʼязково)",
        "Комісія: 30 (необовʼязково)",
        "OLX ID: ABC-1234 (необовʼязково)",
        "20 4514 2425 2955",
        "",
        "Для офлайн-продажу замість ТТН напиши «Офлайн».",
    ]
    if admin:
        lines.extend(
            [
                "",
                "Основні команди:",
                "/add_product — додати новий товар",
            ]
        )
    if owner:
        lines.extend(
            [
                "/stats — продажі, витрати й прибуток",
            ]
        )
    if admin:
        lines.extend(
            [
                "/stock — склад і залишки",
                "/status ТТН — перевірити посилку",
                "/retry ТТН — повторити відстеження",
                "/health — перевірити роботу бота",
            ]
        )
    if owner:
        lines.extend(
            [
                "",
                "Фінанси:",
                "/unlock пароль — відкрити фінансові дані",
                "/turnover live — оборот за поточний місяць",
                "/turnover all — оборот за весь час",
                "/clear_month — очистити лише поточний місяць",
                "/lock — закрити фінансові дані",
                "",
                "Інше:",
                "/clear — прибрати повідомлення в цьому чаті або темі",
            ]
        )
    if admin:
        lines.extend(
            [
                "/export — журнал відправлень CSV",
                "/export1c — файл для 1С",
                "/backup — резервна копія бази",
            ]
        )
    lines.extend(
        [
            "/whoami — показати твій Telegram ID",
            "/help — показати цю підказку",
        ]
    )
    return "\n".join(lines)


def _is_offline(text: str) -> bool:
    low = (text or "").lower()
    return "офлайн" in low or "offline" in low


def _extract_client_name(raw: str, message: Any) -> str:
    lines = [line.strip() for line in (raw or "").splitlines() if line.strip()]
    product_lines = (parse_message(raw).get("description") or "").splitlines()
    if lines:
        first = re.sub(r"^[^\wА-ЯІЇЄа-яіїє]+", "", lines[0]).strip()
        low = first.lower()
        reserved = {
            "фоп",
            "наложка",
            "олх",
            "olx",
            "офлайн",
            "offline",
        }
        if (
            low not in reserved
            and first not in product_lines
            and not re.search(r"\d", first)
            and not low.startswith(
                (
                    "пошта",
                    "сума",
                    "вартість",
                    "ціна",
                    "опис",
                    "тел",
                    "місто",
                    "відділення",
                    "тип",
                )
            )
            and 1 <= len(first.split()) <= 5
        ):
            return first[:100]
    user = getattr(message, "from_user", None)
    full_name = getattr(user, "full_name", None)
    if full_name:
        return str(full_name)[:100]
    username = getattr(user, "username", None)
    return f"@{username}" if username else "Unknown"


def _command_parts(text: str) -> tuple[str, list[str]]:
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", []
    command = parts[0].split("@", 1)[0].lower()
    return command, parts[1:]


def _single_ttn(args: list[str]) -> str | None:
    numbers = extract_ttns(" ".join(args))
    return numbers[0] if len(numbers) == 1 else None


def _handle_status(bot: Any, chat_id: int, args: list[str]) -> None:
    ttn = _single_ttn(args)
    if not ttn:
        bot.send_message(chat_id, "Використання: /status 20451424252955")
        return
    snapshot = fetch_tracking_snapshot(ttn)
    if not snapshot:
        from ukrposhta_api import normalize_ukr_ttn

        text = (
            "⏳ Статус Укрпошти ще не завантажився. Спробуй трохи пізніше."
            if normalize_ukr_ttn(ttn)
            else "Не вдалося знайти цю ТТН у підключених кабінетах Нової пошти."
        )
        bot.send_message(chat_id, text)
        return
    lines = [
        f"🔖 ТТН: {ttn}",
        f"Статус: {snapshot['status_label']}",
    ]
    if snapshot.get("place"):
        lines.append(f"📍 Де: {snapshot['place']}")
    if snapshot.get("profile_name"):
        lines.append(f"Кабінет: {snapshot['profile_name']}")
    bot.send_message(chat_id, "\n".join(lines))


def _handle_retry(bot: Any, chat_id: int, args: list[str]) -> None:
    ttn = _single_ttn(args)
    if not ttn:
        bot.send_message(chat_id, "Використання: /retry 20451424252955")
        return
    shipment = get_shipment(ttn)
    if not shipment:
        bot.send_message(chat_id, "❌ Цієї ТТН немає в робочій базі.")
        return
    reactivate_shipment(ttn)
    ok, snapshot, _changed, mode = refresh_ttn(
        bot,
        ttn,
        preferred_account=shipment.get("profile_name") or None,
        source_text=shipment.get("source_text") or "",
        description=shipment.get("description") or "",
        shipment_kind=shipment.get("shipment_kind") or "Наложка",
        cost_text=shipment.get("cost_text") or "",
        owner_name=shipment.get("account_name") or None,
        source_user_id=shipment.get("source_user_id") or None,
        source_user_name=shipment.get("source_user_name") or None,
        source_chat_id=shipment.get("source_chat_id") or None,
        source_thread_id=shipment.get("source_thread_id") or None,
        source_message_id=shipment.get("source_message_id") or None,
        olx_order_id=shipment.get("olx_order_id") or None,
    )
    if ok and snapshot:
        bot.send_message(
            chat_id, f"✅ Відстеження активовано. Статус: {snapshot['status_label']}"
        )
    else:
        bot.send_message(
            chat_id, f"⚠️ Відстеження активовано, але зараз ТТН не знайдено ({mode})."
        )


def _handle_export(bot: Any, chat_id: int, args: list[str]) -> None:
    try:
        days = int(args[0]) if args else 365
    except ValueError:
        bot.send_message(chat_id, "Використання: /export 365")
        return
    days = max(1, min(days, 3650))
    content = export_shipments_csv(days)
    date = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")
    document = io.BytesIO(content)
    document.name = f"shipments-{date}-{days}d.csv"
    bot.send_document(
        chat_id,
        document,
        caption=f"Експорт робочого журналу за {days} днів.",
    )


def _handle_health(bot: Any, chat_id: int) -> None:
    health = database_health()
    business = business_health()
    bot.send_message(
        chat_id,
        "\n".join(
            [
                "✅ Бот і база працюють.",
                f"База: {database_backend(DATABASE_URL).upper()}",
                f"Усього відправлень: {health['total']}",
                f"Активно відстежуються: {health['active']}",
                f"Записи з помилкою: {health['errors']}",
                f"Бізнес-замовлень: {business['orders']}",
                f"Товарів у довіднику: {business['products']}",
                f"Чернеток в очікуванні: {business['pending']}",
            ]
        ),
    )


def _handle_turnover(bot: Any, chat_id: int, args: list[str]) -> None:
    mode = args[0].lower() if args else ""
    if mode in {"live", "all"}:
        stats = get_sales_stats("month" if mode == "live" else "all")
        period = "поточний місяць" if mode == "live" else "весь час"
        bot.send_message(
            chat_id,
            "\n".join(
                [
                    f"Оборот за {period} 🤍",
                    "",
                    f"{format_money(stats['turnover_cents'])} грн",
                    f"Продажів: {stats['sales_count']}",
                ]
            ),
        )
        return
    month = args[0] if args else datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m")
    try:
        result = get_monthly_turnover(month)
    except ValueError:
        bot.send_message(chat_id, "Використання: /turnover 2026-11")
        return
    if not result:
        bot.send_message(
            chat_id,
            f"Поки немає готового обороту за {month}. Можна перевірити /turnover live.",
        )
        return
    lines = [
        f"Оборот за {month} 🤍",
        "",
        f"{format_money(result['turnover_cents'])} {result['currency']}",
    ]
    if int(result.get("orders_count") or 0):
        lines.append(f"Замовлень: {result['orders_count']}")
    lines.append(f"Розраховано: {result['calculated_at']}")
    if result.get("note"):
        lines.append(f"Примітка: {result['note']}")
    bot.send_message(chat_id, "\n".join(lines))


def _handle_stats(bot: Any, chat_id: int, args: list[str]) -> None:
    period = args[0].lower() if args else "month"
    try:
        stats = get_sales_stats(period)
    except ValueError:
        bot.send_message(chat_id, "Приклад: /stats, /stats 2026-11 або /stats all")
        return
    title = "весь час" if stats["period"] == "all" else stats["period"]
    lines = [
        f"Підсумок за {title} 🤍",
        "",
        f"Продажів: {stats['sales_count']}",
        f"Товарів: {stats['items_count']}",
        f"Оборот: {format_money(stats['turnover_cents'])} грн",
        f"Витрати: {format_money(stats['expenses_cents'])} грн",
        f"Прибуток: {format_money(stats['profit_cents'])} грн",
    ]
    if int(stats.get("cost_pending_count") or 0):
        lines.append(
            "Ще без повного розрахунку: "
            f"{stats['cost_pending_count']} продаж(і) — потрібна собівартість"
        )
    lines.append(f"Відмов: {stats['refusals_count']} ({stats['refusal_rate']:.1f}%)")
    bot.send_message(chat_id, "\n".join(lines))


def _handle_clear_month(bot: Any, chat_id: int, args: list[str]) -> None:
    if args:
        bot.send_message(
            chat_id,
            "Ця команда працює тільки з поточним місяцем. Напиши просто /clear_month",
        )
        return
    month = current_month_label()
    bot.send_message(
        chat_id,
        (
            f"Очистити аналітику за {month}?\n\n"
            "Продажі в 1С та історія складу залишаться. "
            "Оборот і статистика цього місяця почнуть рахуватися з нуля."
        ),
        reply_markup=clear_current_month_keyboard(),
    )


def _handle_unlock(bot: Any, message: Any, user_id: int, text: str) -> None:
    chat_id = message.chat.id
    if not is_owner(user_id):
        bot.send_message(chat_id, "Цей розділ доступний лише власнику.")
        return
    password = (
        text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""
    )
    if not password:
        bot.send_message(chat_id, "Напиши: /unlock твій_пароль")
        return
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        LOGGER.debug("Could not delete password message", exc_info=True)
    status, seconds = ANALYTICS_ACCESS.unlock(user_id, password)
    password = ""
    if status == "ok":
        bot.send_message(
            chat_id,
            f"Фінанси відкрито на {max(1, seconds // 60)} хвилин. 🔓",
        )
    elif status == "cooldown":
        bot.send_message(
            chat_id,
            f"Забагато спроб. Спробуй через {max(1, seconds // 60)} хв.",
        )
    else:
        bot.send_message(chat_id, "Пароль не підійшов.")


def _handle_products(bot: Any, chat_id: int) -> None:
    products = list_products()
    if not products:
        bot.send_message(chat_id, "Довідник товарів поки порожній.")
        return
    lines = ["📦 Товари:"]
    for product in products:
        lines.append(f"• {product['id']} — {product['canonical_name']}")
    bot.send_message(chat_id, "\n".join(lines)[:4000])


def _handle_product_command(bot: Any, chat_id: int, text: str) -> None:
    payload = text.split(maxsplit=2)
    if len(payload) < 3:
        bot.send_message(
            chat_id,
            "Використання:\n/product add Штани Suvene\n"
            "/product rename ID | Нова назва\n/product alias ID | інша назва",
        )
        return
    action, value = payload[1].lower(), payload[2].strip()
    if action == "add":
        product = resolve_product(value, create=True)
        bot.send_message(
            chat_id, f"✅ Товар: {product['canonical_name']}\nID: {product['id']}"
        )
        return
    if "|" not in value:
        bot.send_message(chat_id, "Для rename/alias розділи ID і назву символом |")
        return
    product_id, name = [part.strip() for part in value.split("|", 1)]
    if action == "rename":
        product = rename_product(product_id, name)
        bot.send_message(
            chat_id,
            f"✅ Нова назва: {product['canonical_name']}"
            if product
            else "❌ Товар не знайдено",
        )
    elif action == "alias":
        ok = add_product_alias(product_id, name)
        bot.send_message(
            chat_id, "✅ Іншу назву додано" if ok else "❌ Товар не знайдено"
        )
    else:
        bot.send_message(chat_id, "Доступні дії: add, rename, alias")


def _handle_business_edit(bot: Any, chat_id: int, text: str, user: Any) -> None:
    payload = text.split(maxsplit=2)
    if len(payload) < 3:
        bot.send_message(
            chat_id,
            "Приклад:\n/edit 20451424252955 товар=Штани Suvene; ціна=1200; собівартість=500",
        )
        return
    ttn, raw_changes = payload[1], payload[2]
    key_map = {
        "товар": "product",
        "product": "product",
        "ціна": "price",
        "price": "price",
        "собівартість": "cost",
        "cost": "cost",
        "доставка": "delivery",
        "delivery": "delivery",
        "комісія": "fee",
        "fee": "fee",
        "інше": "other",
        "other": "other",
        "кількість": "quantity",
        "quantity": "quantity",
        "olx": "olx",
    }
    changes: dict[str, str] = {}
    for part in raw_changes.split(";"):
        if "=" not in part:
            continue
        key, value = [piece.strip() for piece in part.split("=", 1)]
        mapped = key_map.get(key.lower())
        if mapped and value:
            changes[mapped] = value
    try:
        order = edit_order_by_ttn(
            ttn,
            changes,
            int(user.id),
            getattr(user, "full_name", None) or "",
        )
    except (ValueError, TypeError):
        bot.send_message(chat_id, "❌ Некоректне значення.")
        return
    bot.send_message(
        chat_id, "✅ Продаж оновлено." if order else "❌ ТТН не знайдено у бізнес-базі."
    )


def _handle_export_1c(bot: Any, chat_id: int, args: list[str]) -> None:
    try:
        days = int(args[0]) if args else 365
    except ValueError:
        bot.send_message(chat_id, "Використання: /export1c 365")
        return
    days = max(1, min(days, 3650))
    document = io.BytesIO(export_1c_csv(days))
    document.name = f"business-1c-{days}d.csv"
    bot.send_document(chat_id, document, caption=f"Бізнес-експорт за {days} днів")


def handle_private_message(bot: Any, message: Any) -> None:
    chat_id = message.chat.id
    text = (
        getattr(message, "text", None) or getattr(message, "caption", None) or ""
    ).strip()
    if not text:
        return
    user = getattr(message, "from_user", None)
    user_id = int(getattr(user, "id", 0) or 0)
    command, args = _command_parts(text)

    if command == "/whoami":
        bot.send_message(chat_id, f"Твій Telegram ID: {user_id}")
        return
    if command in {"/start", "/help"}:
        bot.send_message(
            chat_id,
            private_help_text(admin=is_admin(user_id), owner=is_owner(user_id)),
        )
        return
    if not is_private_user_allowed(user_id):
        LOGGER.warning("Rejected private message from unauthorized user %s", user_id)
        bot.send_message(
            chat_id,
            "⛔ Доступ не дозволено. Надішли адміністратору свій ID через /whoami.",
        )
        return
    if command == "/unlock":
        _handle_unlock(bot, message, user_id, text)
        return
    if command == "/lock":
        if is_owner(user_id):
            ANALYTICS_ACCESS.lock(user_id)
            bot.send_message(chat_id, "Фінанси знову закриті. 🔒")
        else:
            bot.send_message(chat_id, "Цей розділ доступний лише власнику.")
        return
    if command in PROTECTED_DATA_COMMANDS:
        if not is_owner(user_id):
            bot.send_message(chat_id, "Ці дані доступні лише власнику.")
            return
        if not ANALYTICS_ACCESS.is_unlocked(user_id):
            bot.send_message(
                chat_id, "Фінанси закриті. Спочатку напиши /unlock пароль 🔒"
            )
            return
    if command == "/status":
        _handle_status(bot, chat_id, args)
        return
    if command in {
        "/health",
        "/retry",
        "/export",
        "/export1c",
        "/turnover",
        "/oborot",
        "/stats",
        "/clear_month",
        "/stock",
        "/add_product",
        "/products",
        "/product",
        "/edit",
        "/backup",
    }:
        if not is_admin(user_id):
            bot.send_message(chat_id, "⛔ Ця команда доступна лише адміністратору.")
            return
        if command == "/health":
            _handle_health(bot, chat_id)
        elif command == "/retry":
            _handle_retry(bot, chat_id, args)
        elif command == "/export":
            _handle_export(bot, chat_id, args)
        elif command == "/export1c":
            _handle_export_1c(bot, chat_id, args)
        elif command in {"/turnover", "/oborot"}:
            _handle_turnover(bot, chat_id, args)
        elif command == "/stats":
            _handle_stats(bot, chat_id, args)
        elif command == "/clear_month":
            _handle_clear_month(bot, chat_id, args)
        elif command == "/stock":
            send_inventory_menu(bot, chat_id, user_id)
        elif command == "/add_product":
            start_inventory_product_creation(bot, chat_id, user_id)
        elif command == "/products":
            _handle_products(bot, chat_id)
        elif command == "/product":
            _handle_product_command(bot, chat_id, text)
        elif command == "/edit":
            _handle_business_edit(bot, chat_id, text, user)
        elif command == "/backup":
            try:
                path = create_backup()
            except Exception as exc:
                LOGGER.exception("Manual database backup failed")
                bot.send_message(
                    chat_id, f"⚠️ Не вдалося створити копію: {type(exc).__name__}"
                )
            else:
                text = (
                    f"✅ Резервна копія: {path.name}"
                    if path
                    else "⚠️ Базу ще не створено"
                )
                bot.send_message(chat_id, text)
        return
    if command:
        bot.send_message(chat_id, "Не впізнав цю команду. Підказка є в /help.")
        return
    if is_admin(user_id) and handle_inventory_text(bot, message):
        return

    parsed = parse_message(text)
    ttns = extract_ttns(text)
    offline = _is_offline(text)
    errors = validate(
        {
            **parsed,
            "ttns": ttns,
            "offline": offline,
        }
    )
    if errors:
        bot.send_message(chat_id, "\n".join(errors))
        return

    forward_text = build_employee_text(
        client_name=_extract_client_name(text, message),
        user_id=user_id,
        shipment_kind=parsed.get("shipment_kind")
        or ("Офлайн" if offline else "Наложка"),
        description=parsed.get("description") or "",
        cost_text=parsed.get("cost_text") or "",
        ttns=ttns,
        offline=offline,
        olx_order_id=parsed.get("olx_order_id") or "",
        quantity=parsed.get("quantity") or "1",
        purchase_cost=parsed.get("purchase_cost") or "",
        delivery_cost=parsed.get("delivery_cost") or "",
        platform_fee=parsed.get("platform_fee") or "",
        other_cost=parsed.get("other_cost") or "",
    )
    submission_id = create_submission(
        text,
        {**parsed, "offline": offline},
        ttns,
        user_id,
        getattr(user, "full_name", None) or getattr(user, "username", None) or "",
    )
    forwarded = bot.send_message(
        TG_GROUP_ID,
        forward_text,
        message_thread_id=TG_EMPLOYEES_THREAD_ID,
        reply_markup=submission_keyboard(submission_id, offline=offline),
    )
    attach_submission_message(submission_id, TG_GROUP_ID, forwarded.message_id)
    summary = "Замовлення додано в «До відправки» 📦\nПеревір картку й натисни «Підтвердити замовлення»."
    if offline:
        summary = "Готово — продаж чекає підтвердження оплати. 🤍"
    if ttns:
        summary += "\nТТН: " + ", ".join(ttns)
    bot.send_message(chat_id, summary)
