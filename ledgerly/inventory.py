"""Admin-only Telegram inventory menu and persistent setup wizard."""

from __future__ import annotations

import math
import re
from typing import Any

from telebot import types
from telebot.apihelper import ApiTelegramException

from business_db import (
    COLOR_LABELS,
    SIZE_CODES,
    TRANSPORT_LABELS,
    archive_inventory_product,
    change_inventory_stock,
    clear_inventory_wizard,
    configure_product_variants,
    create_inventory_product,
    format_money,
    get_inventory_product,
    get_inventory_variant,
    get_inventory_wizard,
    get_last_reversible_inventory_movement,
    get_product_transport_costs,
    get_product_variant_options,
    list_inventory_products,
    list_product_variants,
    receive_inventory_batch,
    rename_product,
    save_inventory_wizard,
    set_inventory_stock,
    set_product_transport_cost,
    undo_inventory_movement,
)

PAGE_SIZE = 8
COLOR_EMOJI = {"BLACK": "⚫", "WHITE": "⚪", "GRAY": "🔘"}
TRANSPORT_EMOJI = {"SEA": "🚢", "AIR": "✈️"}


def _actor_name(user: Any) -> str:
    return str(getattr(user, "full_name", None) or getattr(user, "username", "") or "")


def _menu_keyboard() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("➕ Додати товар", callback_data="inv:add:start")
    )
    keyboard.add(
        types.InlineKeyboardButton("📦 Залишки", callback_data="inv:list:view:0"),
        types.InlineKeyboardButton(
            "📥 Прийняти товар", callback_data="inv:list:receive:0"
        ),
    )
    keyboard.add(
        types.InlineKeyboardButton(
            "➖ Списати товар", callback_data="inv:list:adjust:0"
        ),
        types.InlineKeyboardButton(
            "⚙️ Налаштування", callback_data="inv:list:edit:0"
        ),
    )
    keyboard.add(
        types.InlineKeyboardButton(
            "↩️ Скасувати останню дію", callback_data="inv:undo:x"
        )
    )
    return keyboard


def send_inventory_menu(bot: Any, chat_id: int, user_id: int) -> None:
    clear_inventory_wizard(user_id)
    bot.send_message(
        chat_id,
        "📦 Склад\n\nДодай товар, перевір залишки або зміни кількість.",
        reply_markup=_menu_keyboard(),
    )


def start_inventory_product_creation(bot: Any, chat_id: int, user_id: int) -> None:
    save_inventory_wizard(user_id, chat_id, "NEW_NAME", {})
    bot.send_message(
        chat_id,
        "Напиши назву нового товару одним повідомленням.\n\nНаприклад: Штани Broken Planet",
        reply_markup=_cancel_keyboard(),
    )


def _cancel_keyboard() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("Скасувати", callback_data="inv:cancel:x"))
    return keyboard


def _edit_or_send(
    bot: Any,
    call: Any,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
) -> None:
    try:
        bot.edit_message_text(
            text=text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=reply_markup,
        )
    except ApiTelegramException as exc:
        if "message is not modified" in str(exc).casefold():
            return
        bot.send_message(call.message.chat.id, text, reply_markup=reply_markup)


def _product_list_text(products: list[dict[str, Any]], mode: str) -> str:
    titles = {
        "view": "📦 Залишки на складі",
        "receive": "📥 Прийняти товар",
        "adjust": "➖ Списати товар",
        "edit": "⚙️ Налаштування товарів",
    }
    lines = [titles.get(mode, "Склад"), ""]
    if not products:
        lines.append("Товарів ще немає. Натисни «➕ Додати товар».")
        return "\n".join(lines)
    for product in products:
        available = int(product.get("available") or 0)
        awaiting = int(product.get("awaiting_shipment") or 0)
        in_transit = int(product.get("in_transit") or 0)
        suffix = (
            f"доступно {available} · резерв {awaiting} · у дорозі {in_transit}"
            if int(product.get("variant_count") or 0)
            else "не налаштовано"
        )
        lines.append(f"• {product['canonical_name']} — {suffix}")
    return "\n".join(lines)[:4000]


def _product_list_keyboard(
    products: list[dict[str, Any]],
    mode: str,
    page: int,
) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    total_pages = max(1, math.ceil(len(products) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    for product in products[start : start + PAGE_SIZE]:
        available = int(product.get("available") or 0)
        awaiting = int(product.get("awaiting_shipment") or 0)
        in_transit = int(product.get("in_transit") or 0)
        label = (
            f"{product['canonical_name']} · доступно {available} · "
            f"резерв {awaiting} · у дорозі {in_transit}"
        )
        if not int(product.get("variant_count") or 0):
            label = f"{product['canonical_name']} · налаштувати"
        keyboard.add(
            types.InlineKeyboardButton(
                label[:60],
                callback_data=f"inv:product:{mode}:{product['id']}",
            )
        )
    nav: list[types.InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            types.InlineKeyboardButton("←", callback_data=f"inv:list:{mode}:{page - 1}")
        )
    if page + 1 < total_pages:
        nav.append(
            types.InlineKeyboardButton("→", callback_data=f"inv:list:{mode}:{page + 1}")
        )
    if nav:
        keyboard.row(*nav)
    keyboard.add(types.InlineKeyboardButton("← До меню складу", callback_data="inv:menu:root"))
    return keyboard


def _inventory_product_text(product_id: str) -> str:
    product = get_inventory_product(product_id)
    if not product:
        return "Товар не знайдено."
    variants = list_product_variants(product_id)
    lines = [f"📦 {product['canonical_name']}", ""]
    if not variants:
        lines.append("Розміри й кольори ще не налаштовані.")
        return "\n".join(lines)
    on_hand = sum(int(row.get("on_hand") or 0) for row in variants)
    awaiting = sum(int(row.get("awaiting_shipment") or 0) for row in variants)
    in_transit = sum(int(row.get("in_transit") or 0) for row in variants)
    lines.extend(
        [
            f"На складі: {on_hand}",
            f"Зарезервовано: {awaiting}",
            f"У дорозі до клієнтів: {in_transit}",
            f"Доступно для продажу: {on_hand - awaiting - in_transit}",
            "",
        ]
    )
    for color in COLOR_LABELS:
        rows = [row for row in variants if row["color_code"] == color]
        if not rows:
            continue
        lines.append(f"{COLOR_EMOJI[color]} {COLOR_LABELS[color]}")
        lines.append(
            "  " + " · ".join(f"{row['size_code']}: {row['available']}" for row in rows)
        )
    costs = get_product_transport_costs(product_id)
    lines.extend(["", "Собівартість за 1 шт."])
    for mode, label in TRANSPORT_LABELS.items():
        value = costs.get(mode)
        price = f"{format_money(value)} грн" if value else "не вказана"
        lines.append(f"{TRANSPORT_EMOJI[mode]} {label}: {price}")
    return "\n".join(lines)[:4000]


def _product_detail_keyboard(product_id: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "📥 Прийняти товар", callback_data=f"inv:receivep:x:{product_id}"
        ),
        types.InlineKeyboardButton(
            "➖ Списати товар", callback_data=f"inv:adjustp:x:{product_id}"
        ),
    )
    keyboard.add(
        types.InlineKeyboardButton(
            "⚙️ Налаштувати", callback_data=f"inv:editp:x:{product_id}"
        ),
        types.InlineKeyboardButton("До списку", callback_data="inv:list:view:0"),
    )
    return keyboard


def _edit_product_keyboard(product_id: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "Змінити назву", callback_data=f"inv:rename:x:{product_id}"
        ),
        types.InlineKeyboardButton(
            "Розміри й кольори", callback_data=f"inv:options:x:{product_id}"
        ),
    )
    keyboard.add(
        types.InlineKeyboardButton(
            "Собівартість", callback_data=f"inv:costs:x:{product_id}"
        ),
        types.InlineKeyboardButton(
            "Залишки", callback_data=f"inv:viewp:x:{product_id}"
        ),
    )
    keyboard.add(
        types.InlineKeyboardButton(
            "🗑 Видалити товар", callback_data=f"inv:deletep:x:{product_id}"
        )
    )
    keyboard.add(
        types.InlineKeyboardButton("До списку", callback_data="inv:list:edit:0"),
    )
    return keyboard


def _variant_text(variant: dict[str, Any]) -> str:
    color = COLOR_LABELS.get(variant["color_code"], variant["color_code"])
    return "\n".join(
        [
            f"📦 {variant['product_name']}",
            f"{COLOR_EMOJI.get(variant['color_code'], '•')} {color} · {variant['size_code']}",
            "",
            f"На складі: {variant['on_hand']}",
            f"Зарезервовано: {variant.get('awaiting_shipment', 0)}",
            f"У дорозі до клієнтів: {variant.get('in_transit', 0)}",
            f"Доступно для продажу: {variant['available']}",
        ]
    )


def _variant_keyboard(
    variant: dict[str, Any], movement_id: str = ""
) -> types.InlineKeyboardMarkup:
    variant_id = variant["id"]
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "Списати 1", callback_data=f"inv:delta:{variant_id}:-1"
        ),
        types.InlineKeyboardButton(
            "Списати 5", callback_data=f"inv:delta:{variant_id}:-5"
        ),
    )
    keyboard.add(
        types.InlineKeyboardButton(
            "Списати іншу кількість",
            callback_data=f"inv:writeoffq:x:{variant_id}",
        )
    )
    keyboard.add(
        types.InlineKeyboardButton(
            "Вказати точну кількість",
            callback_data=f"inv:set:x:{variant_id}",
        )
    )
    if movement_id:
        keyboard.add(
            types.InlineKeyboardButton(
                "↩️ Скасувати цю зміну",
                callback_data=f"inv:undoask:x:{movement_id}",
            )
        )
    keyboard.add(
        types.InlineKeyboardButton(
            "До розмірів і кольорів",
            callback_data=f"inv:adjustp:x:{variant['product_id']}",
        )
    )
    return keyboard


def _delete_product_keyboard(product_id: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "Так, видалити", callback_data=f"inv:deleteok:x:{product_id}"
        ),
        types.InlineKeyboardButton(
            "Ні, залишити", callback_data=f"inv:editp:x:{product_id}"
        ),
    )
    return keyboard


def _variants_keyboard(
    product_id: str, mode: str = "adjust"
) -> types.InlineKeyboardMarkup:
    variants = list_product_variants(product_id)
    callback_action = "recvvar" if mode == "receive" else "variant"
    keyboard = types.InlineKeyboardMarkup(row_width=3)
    buttons = [
        types.InlineKeyboardButton(
            f"{COLOR_EMOJI.get(row['color_code'], '•')} {row['size_code']} · {row['available']}",
            callback_data=f"inv:{callback_action}:x:{row['id']}",
        )
        for row in variants
    ]
    for index in range(0, len(buttons), 3):
        keyboard.row(*buttons[index : index + 3])
    back_mode = "receive" if mode == "receive" else "adjust"
    keyboard.add(
        types.InlineKeyboardButton("До списку", callback_data=f"inv:list:{back_mode}:0")
    )
    return keyboard


def _costs_text(product_id: str) -> str:
    product = get_inventory_product(product_id)
    if not product:
        return "Товар не знайдено."
    costs = get_product_transport_costs(product_id)
    lines = [
        f"💰 {product['canonical_name']}",
        "",
        "Собівартість 1 шт. разом із доставкою:",
    ]
    for mode, label in TRANSPORT_LABELS.items():
        value = costs.get(mode)
        price = f"{format_money(value)} грн" if value else "ще не вказана"
        lines.append(f"{TRANSPORT_EMOJI[mode]} {label}: {price}")
    lines.extend(
        [
            "",
            "Нові надходження отримають цю собівартість. Уже додані надходження не зміняться.",
        ]
    )
    return "\n".join(lines)


def _costs_keyboard(product_id: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    for mode, label in TRANSPORT_LABELS.items():
        keyboard.add(
            types.InlineKeyboardButton(
                f"{TRANSPORT_EMOJI[mode]} Ціна — {label.lower()}",
                callback_data=f"inv:setcost:{mode}:{product_id}",
            )
        )
    keyboard.add(
        types.InlineKeyboardButton(
            "До товару", callback_data=f"inv:editp:x:{product_id}"
        )
    )
    return keyboard


def _receipt_method_text(variant: dict[str, Any]) -> str:
    color = COLOR_LABELS.get(variant["color_code"], variant["color_code"])
    return "\n".join(
        [
            f"📦 {variant['product_name']}",
            f"{COLOR_EMOJI.get(variant['color_code'], '•')} {color} · {variant['size_code']}",
            "",
            "Як доставили цей товар?",
            "Море або літак впливає тільки на собівартість.",
        ]
    )


def _receipt_method_keyboard(variant: dict[str, Any]) -> types.InlineKeyboardMarkup:
    costs = get_product_transport_costs(variant["product_id"])
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for mode, label in TRANSPORT_LABELS.items():
        value = costs.get(mode)
        suffix = f"{format_money(value)} грн/шт." if value else "вказати ціну"
        keyboard.add(
            types.InlineKeyboardButton(
                f"{TRANSPORT_EMOJI[mode]} {label} · {suffix}",
                callback_data=f"inv:method:{mode}:{variant['id']}",
            )
        )
    keyboard.add(
        types.InlineKeyboardButton(
            "До розмірів і кольорів",
            callback_data=f"inv:receivep:x:{variant['product_id']}",
        )
    )
    return keyboard


def _receipt_quantity_text(variant: dict[str, Any], mode: str) -> str:
    costs = get_product_transport_costs(variant["product_id"])
    unit_cost = costs.get(mode)
    color = COLOR_LABELS.get(variant["color_code"], variant["color_code"])
    return "\n".join(
        [
            f"{TRANSPORT_EMOJI[mode]} {TRANSPORT_LABELS[mode]}",
            f"📦 {variant['product_name']} · {color} · {variant['size_code']}",
            f"Собівартість: {format_money(unit_cost)} грн/шт.",
            "",
            "Скільки штук приїхало? Напиши кількість одним числом.",
        ]
    )


def _receipt_quantity_keyboard(
    variant: dict[str, Any],
    mode: str,
) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton(
            "Назад",
            callback_data=f"inv:recvvar:x:{variant['id']}",
        )
    )
    return keyboard


def _receipt_result_text(result: dict[str, Any]) -> str:
    color = COLOR_LABELS.get(result["color_code"], result["color_code"])
    mode = result["transport_mode"]
    return "\n".join(
        [
            "Надходження додано ✓",
            "",
            f"📦 {result['product_name']} · {color} · {result['size_code']}",
            f"{TRANSPORT_EMOJI[mode]} {TRANSPORT_LABELS[mode]} · +{result['received_quantity']} шт.",
            f"Собівартість: {format_money(result['unit_cost_cents'])} грн/шт.",
            f"Загальна собівартість: {format_money(result['batch_cost_cents'])} грн",
            f"На складі: {result['on_hand']} шт.",
            "",
            "Готово. Можна продавати.",
        ]
    )


def _receipt_result_keyboard(result: dict[str, Any]) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    movement_id = str(result.get("movement_id") or "")
    if movement_id:
        keyboard.add(
            types.InlineKeyboardButton(
                "↩️ Скасувати це надходження",
                callback_data=f"inv:undoask:x:{movement_id}",
            )
        )
    keyboard.add(
        types.InlineKeyboardButton(
            "Додати ще",
            callback_data=f"inv:receivep:x:{result['product_id']}",
        ),
        types.InlineKeyboardButton(
            "До товару",
            callback_data=f"inv:viewp:x:{result['product_id']}",
        ),
    )
    return keyboard


def _undo_confirmation_keyboard(movement_id: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "Так, скасувати", callback_data=f"inv:undook:x:{movement_id}"
        ),
        types.InlineKeyboardButton("Ні", callback_data="inv:menu:root"),
    )
    return keyboard


def _undo_confirmation_text(movement: dict[str, Any]) -> str:
    color = COLOR_LABELS.get(movement["color_code"], movement["color_code"])
    delta = int(movement["quantity_delta"])
    action = (
        f"додано {delta} шт."
        if delta > 0
        else f"списано {abs(delta)} шт."
    )
    return "\n".join(
        [
            "↩️ Скасувати останню складську дію?",
            "",
            f"📦 {movement['product_name']} · {color} · {movement['size_code']}",
            f"Було {action}",
            "",
            "Склад повернеться до попереднього стану. Продажі ця дія не скасовує.",
        ]
    )


def _options_text(payload: dict[str, Any]) -> str:
    sizes = payload.get("sizes") or []
    colors = payload.get("colors") or []
    return "\n".join(
        [
            f"📦 {payload.get('name') or 'Товар'}",
            "",
            "Обери дозволені розміри й кольори.",
            f"Розміри: {', '.join(sizes) if sizes else 'не вибрано'}",
            "Кольори: "
            + (
                ", ".join(COLOR_LABELS[color] for color in colors)
                if colors
                else "не вибрано"
            ),
        ]
    )


def _options_keyboard(payload: dict[str, Any], mode: str) -> types.InlineKeyboardMarkup:
    selected_sizes = set(payload.get("sizes") or [])
    selected_colors = set(payload.get("colors") or [])
    keyboard = types.InlineKeyboardMarkup(row_width=5)
    keyboard.row(
        *[
            types.InlineKeyboardButton(
                ("✓ " if size in selected_sizes else "") + size,
                callback_data=f"inv:toggle:size:{size}",
            )
            for size in SIZE_CODES
        ]
    )
    keyboard.row(
        *[
            types.InlineKeyboardButton(
                ("✓ " if color in selected_colors else "") + COLOR_LABELS[color],
                callback_data=f"inv:toggle:color:{color}",
            )
            for color in COLOR_LABELS
        ]
    )
    keyboard.add(
        types.InlineKeyboardButton("Зберегти", callback_data=f"inv:save:{mode}"),
        types.InlineKeyboardButton("Скасувати", callback_data="inv:cancel:x"),
    )
    return keyboard


def _show_product_list(bot: Any, call: Any, mode: str, page: int) -> None:
    products = list_inventory_products()
    _edit_or_send(
        bot,
        call,
        _product_list_text(products, mode),
        _product_list_keyboard(products, mode, page),
    )
    bot.answer_callback_query(call.id)


def _show_product(bot: Any, call: Any, product_id: str) -> None:
    _edit_or_send(
        bot,
        call,
        _inventory_product_text(product_id),
        _product_detail_keyboard(product_id),
    )
    bot.answer_callback_query(call.id)


def _show_adjust_product(bot: Any, call: Any, product_id: str) -> None:
    variants = list_product_variants(product_id)
    if not variants:
        _edit_or_send(
            bot,
            call,
            _inventory_product_text(product_id)
            + "\n\nСпочатку налаштуй розміри й кольори.",
            _edit_product_keyboard(product_id),
        )
    else:
        _edit_or_send(
            bot,
            call,
            _inventory_product_text(product_id) + "\n\nОбери колір і розмір:",
            _variants_keyboard(product_id),
        )
    bot.answer_callback_query(call.id)


def _show_receive_product(bot: Any, call: Any, product_id: str) -> None:
    variants = list_product_variants(product_id)
    if not variants:
        _edit_or_send(
            bot,
            call,
            _inventory_product_text(product_id)
            + "\n\nСпочатку налаштуй розміри й кольори.",
            _edit_product_keyboard(product_id),
        )
    else:
        _edit_or_send(
            bot,
            call,
            _inventory_product_text(product_id) + "\n\nОбери, що приїхало:",
            _variants_keyboard(product_id, "receive"),
        )
    bot.answer_callback_query(call.id)


def handle_inventory_callback(bot: Any, call: Any) -> None:
    data = str(call.data or "")
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "inv":
        bot.answer_callback_query(call.id, "Некоректна дія", show_alert=True)
        return
    action = parts[1]
    user_id = int(call.from_user.id)
    chat_id = int(call.message.chat.id)

    if action in {'list','product','viewp','adjustp','receivep','editp','variant','recvvar'}:
        # Leaving an input screen must not leave its old stock mutation armed.
        clear_inventory_wizard(user_id)

    if action == "menu":
        clear_inventory_wizard(user_id)
        _edit_or_send(
            bot,
            call,
            "📦 Склад\n\nДодай товар, перевір залишки або зміни кількість.",
            _menu_keyboard(),
        )
        bot.answer_callback_query(call.id)
        return
    if action == "cancel":
        clear_inventory_wizard(user_id)
        _edit_or_send(
            bot, call, "Дію скасовано.\n\nОбери, що потрібно зробити.", _menu_keyboard()
        )
        bot.answer_callback_query(call.id, "Скасовано")
        return
    if action in {"undo", "undoask"}:
        movement = get_last_reversible_inventory_movement()
        requested_id = parts[3] if action == "undoask" and len(parts) == 4 else ""
        if not movement:
            bot.answer_callback_query(
                call.id, "Немає складської дії, яку можна скасувати", show_alert=True
            )
            return
        if requested_id and movement["id"] != requested_id:
            bot.answer_callback_query(
                call.id,
                "Після цієї дії склад уже змінювався. Відкрий скасування з меню складу",
                show_alert=True,
            )
            return
        _edit_or_send(
            bot,
            call,
            _undo_confirmation_text(movement),
            _undo_confirmation_keyboard(movement["id"]),
        )
        bot.answer_callback_query(call.id)
        return
    if action == "undook" and len(parts) == 4:
        try:
            variant = undo_inventory_movement(
                parts[3], user_id, _actor_name(call.from_user)
            )
        except ValueError as exc:
            bot.answer_callback_query(call.id, str(exc), show_alert=True)
            return
        _edit_or_send(
            bot,
            call,
            "Останню складську дію скасовано ✓\n\n" + _variant_text(variant),
            _variant_keyboard(variant),
        )
        bot.answer_callback_query(call.id, "Склад повернуто до попереднього стану")
        return
    if action == "list" and len(parts) == 4:
        _show_product_list(bot, call, parts[2], int(parts[3]))
        return
    if action == "product" and len(parts) == 4:
        mode, product_id = parts[2], parts[3]
        if mode == "view":
            _show_product(bot, call, product_id)
        elif mode == "receive":
            _show_receive_product(bot, call, product_id)
        elif mode == "adjust":
            _show_adjust_product(bot, call, product_id)
        else:
            product = get_inventory_product(product_id)
            if not product:
                bot.answer_callback_query(call.id, "Товар не знайдено", show_alert=True)
                return
            _edit_or_send(
                bot,
                call,
                f"📦 {product['canonical_name']}\n\nЩо змінити?",
                _edit_product_keyboard(product_id),
            )
            bot.answer_callback_query(call.id)
        return
    if action == "viewp" and len(parts) == 4:
        _show_product(bot, call, parts[3])
        return
    if action == "adjustp" and len(parts) == 4:
        _show_adjust_product(bot, call, parts[3])
        return
    if action == "receivep" and len(parts) == 4:
        _show_receive_product(bot, call, parts[3])
        return
    if action == "editp" and len(parts) == 4:
        product_id = parts[3]
        product = get_inventory_product(product_id)
        if not product:
            bot.answer_callback_query(call.id, "Товар не знайдено", show_alert=True)
            return
        _edit_or_send(
            bot,
            call,
            f"📦 {product['canonical_name']}\n\nЩо змінити?",
            _edit_product_keyboard(product_id),
        )
        bot.answer_callback_query(call.id)
        return
    if action == "deletep" and len(parts) == 4:
        product = get_inventory_product(parts[3])
        if not product:
            bot.answer_callback_query(call.id, "Товар не знайдено", show_alert=True)
            return
        _edit_or_send(
            bot,
            call,
            (
                f"🗑 Видалити «{product['canonical_name']}»?\n\n"
                "Товар зникне зі складу та списків. Старі продажі й статистика залишаться.\n\n"
                "Видалення можливе лише тоді, коли на складі 0 шт."
            ),
            _delete_product_keyboard(product["id"]),
        )
        bot.answer_callback_query(call.id)
        return
    if action == "deleteok" and len(parts) == 4:
        try:
            product = archive_inventory_product(parts[3])
        except ValueError as exc:
            bot.answer_callback_query(call.id, str(exc), show_alert=True)
            return
        clear_inventory_wizard(user_id)
        _edit_or_send(
            bot,
            call,
            (
                f"Товар «{product['canonical_name']}» видалено ✓\n\n"
                "Старі продажі та статистика збережені."
            ),
            _menu_keyboard(),
        )
        bot.answer_callback_query(call.id, "Товар видалено")
        return
    if action == "add":
        save_inventory_wizard(user_id, chat_id, "NEW_NAME", {})
        _edit_or_send(
            bot,
            call,
            "Напиши назву нового товару одним повідомленням.\n\nНаприклад: Штани Broken Planet",
            _cancel_keyboard(),
        )
        bot.answer_callback_query(call.id)
        return
    if action == "rename" and len(parts) == 4:
        product = get_inventory_product(parts[3])
        if not product:
            bot.answer_callback_query(call.id, "Товар не знайдено", show_alert=True)
            return
        save_inventory_wizard(user_id, chat_id, "RENAME", {"product_id": product["id"]})
        _edit_or_send(
            bot,
            call,
            f"Поточна назва: {product['canonical_name']}\n\nНадішли нову назву одним повідомленням.",
            _cancel_keyboard(),
        )
        bot.answer_callback_query(call.id)
        return
    if action == "options" and len(parts) == 4:
        product = get_inventory_product(parts[3])
        if not product:
            bot.answer_callback_query(call.id, "Товар не знайдено", show_alert=True)
            return
        options = get_product_variant_options(product["id"])
        payload = {
            "product_id": product["id"],
            "name": product["canonical_name"],
            **options,
        }
        save_inventory_wizard(user_id, chat_id, "EDIT_OPTIONS", payload)
        _edit_or_send(
            bot, call, _options_text(payload), _options_keyboard(payload, "edit")
        )
        bot.answer_callback_query(call.id)
        return
    if action == "costs" and len(parts) == 4:
        product = get_inventory_product(parts[3])
        if not product:
            bot.answer_callback_query(call.id, "Товар не знайдено", show_alert=True)
            return
        _edit_or_send(
            bot,
            call,
            _costs_text(product["id"]),
            _costs_keyboard(product["id"]),
        )
        bot.answer_callback_query(call.id)
        return
    if action == "setcost" and len(parts) == 4:
        mode, product_id = parts[2], parts[3]
        if mode not in TRANSPORT_LABELS or not get_inventory_product(product_id):
            bot.answer_callback_query(
                call.id, "Некоректний товар або спосіб", show_alert=True
            )
            return
        save_inventory_wizard(
            user_id,
            chat_id,
            "SET_PRODUCT_COST",
            {"product_id": product_id, "transport_mode": mode},
        )
        _edit_or_send(
            bot,
            call,
            (
                f"{TRANSPORT_EMOJI[mode]} {TRANSPORT_LABELS[mode]}\n\n"
                "Напиши собівартість однієї речі вже з доставкою.\n"
                "Наприклад: 420 або 420,50"
            ),
            _cancel_keyboard(),
        )
        bot.answer_callback_query(call.id)
        return
    if action == "toggle" and len(parts) == 4:
        wizard = get_inventory_wizard(user_id)
        if not wizard or wizard["step"] not in {"NEW_OPTIONS", "EDIT_OPTIONS"}:
            bot.answer_callback_query(
                call.id, "Налаштування вже закрито", show_alert=True
            )
            return
        group, value = parts[2], parts[3]
        payload = wizard["payload"]
        key = "sizes" if group == "size" else "colors"
        selected = list(payload.get(key) or [])
        if value in selected:
            selected.remove(value)
        else:
            selected.append(value)
        order = SIZE_CODES if key == "sizes" else tuple(COLOR_LABELS)
        payload[key] = [item for item in order if item in selected]
        save_inventory_wizard(user_id, chat_id, wizard["step"], payload)
        mode = "new" if wizard["step"] == "NEW_OPTIONS" else "edit"
        _edit_or_send(
            bot, call, _options_text(payload), _options_keyboard(payload, mode)
        )
        bot.answer_callback_query(call.id)
        return
    if action == "save" and len(parts) == 3:
        wizard = get_inventory_wizard(user_id)
        if not wizard or wizard["step"] not in {"NEW_OPTIONS", "EDIT_OPTIONS"}:
            bot.answer_callback_query(
                call.id, "Налаштування вже закрито", show_alert=True
            )
            return
        payload = wizard["payload"]
        if not payload.get("sizes") or not payload.get("colors"):
            bot.answer_callback_query(
                call.id,
                "Обери хоча б один розмір і один колір",
                show_alert=True,
            )
            return
        try:
            if wizard["step"] == "NEW_OPTIONS":
                product = create_inventory_product(
                    payload["name"], payload["sizes"], payload["colors"]
                )
            else:
                product = configure_product_variants(
                    payload["product_id"], payload["sizes"], payload["colors"]
                )
        except ValueError as exc:
            bot.answer_callback_query(call.id, str(exc), show_alert=True)
            return
        if wizard["step"] == "NEW_OPTIONS":
            save_inventory_wizard(
                user_id,
                chat_id,
                "NEW_COST_SEA",
                {"product_id": product["id"]},
            )
            _edit_or_send(
                bot,
                call,
                (
                    f"Товар «{product['canonical_name']}» створено ✓\n\n"
                    "Крок 1 із 2 — 🚢 доставка морем\n"
                    "Напиши собівартість 1 шт. разом із доставкою.\n\n"
                    "Наприклад: 420 або 420,50"
                ),
                _cancel_keyboard(),
            )
            bot.answer_callback_query(call.id, "Товар створено")
            return
        clear_inventory_wizard(user_id)
        _edit_or_send(
            bot,
            call,
            _inventory_product_text(product["id"]),
            _product_detail_keyboard(product["id"]),
        )
        bot.answer_callback_query(call.id, "Зміни збережено")
        return
    if action == "variant" and len(parts) == 4:
        variant = get_inventory_variant(parts[3])
        if not variant or not int(variant.get("active") or 0):
            bot.answer_callback_query(
                call.id, "Колір або розмір не знайдено", show_alert=True
            )
            return
        _edit_or_send(bot, call, _variant_text(variant), _variant_keyboard(variant))
        bot.answer_callback_query(call.id)
        return
    if action == "recvvar" and len(parts) == 4:
        variant = get_inventory_variant(parts[3])
        if not variant or not int(variant.get("active") or 0):
            bot.answer_callback_query(
                call.id, "Колір або розмір не знайдено", show_alert=True
            )
            return
        _edit_or_send(
            bot,
            call,
            _receipt_method_text(variant),
            _receipt_method_keyboard(variant),
        )
        bot.answer_callback_query(call.id)
        return
    if action == "method" and len(parts) == 4:
        mode, variant_id = parts[2], parts[3]
        variant = get_inventory_variant(variant_id)
        if (
            mode not in TRANSPORT_LABELS
            or not variant
            or not int(variant.get("active") or 0)
        ):
            bot.answer_callback_query(
                call.id, "Не вдалося знайти цей колір і розмір", show_alert=True
            )
            return
        costs = get_product_transport_costs(variant["product_id"])
        if not costs.get(mode):
            save_inventory_wizard(
                user_id,
                chat_id,
                "SET_RECEIPT_COST",
                {"variant_id": variant_id, "transport_mode": mode},
            )
            _edit_or_send(
                bot,
                call,
                (
                    f"{TRANSPORT_EMOJI[mode]} Ціну для способу «{TRANSPORT_LABELS[mode]}» "
                    "ще не вказано.\n\nНапиши собівартість однієї речі вже з доставкою."
                ),
                _cancel_keyboard(),
            )
        else:
            save_inventory_wizard(
                user_id, chat_id, "RECEIPT_QUANTITY",
                {"variant_id": variant_id, "transport_mode": mode},
            )
            _edit_or_send(
                bot,
                call,
                _receipt_quantity_text(variant, mode),
                _receipt_quantity_keyboard(variant, mode),
            )
        bot.answer_callback_query(call.id)
        return
    if action == "receive" and len(parts) == 5:
        # An old +1/+5/+10 message opens text entry; it never adds stock invisibly.
        call.data = f'inv:receiveq:{parts[4]}:{parts[3]}'
        handle_inventory_callback(bot,call)
        return
    if action == "receiveq" and len(parts) == 4:
        mode, variant_id = parts[2], parts[3]
        variant = get_inventory_variant(variant_id)
        if mode not in TRANSPORT_LABELS or not variant:
            bot.answer_callback_query(
                call.id, "Не вдалося знайти цей колір і розмір", show_alert=True
            )
            return
        save_inventory_wizard(
            user_id,
            chat_id,
            "RECEIPT_QUANTITY",
            {"variant_id": variant_id, "transport_mode": mode},
        )
        _edit_or_send(
            bot,
            call,
            _receipt_quantity_text(variant, mode) + "\nНапиши кількість одним числом.",
            _cancel_keyboard(),
        )
        bot.answer_callback_query(call.id)
        return
    if action == "writeoffq" and len(parts) == 4:
        variant = get_inventory_variant(parts[3])
        if not variant or not int(variant.get("active") or 0):
            bot.answer_callback_query(
                call.id, "Колір або розмір не знайдено", show_alert=True
            )
            return
        save_inventory_wizard(
            user_id,
            chat_id,
            "WRITE_OFF_QUANTITY",
            {"variant_id": variant["id"]},
        )
        _edit_or_send(
            bot,
            call,
            _variant_text(variant)
            + "\n\nСкільки штук списати зі складу?\nЦе не продаж — оберт не зміниться.",
            _cancel_keyboard(),
        )
        bot.answer_callback_query(call.id)
        return
    if action == "delta" and len(parts) == 4:
        try:
            delta = int(parts[3])
            variant = change_inventory_stock(
                parts[2],
                delta,
                user_id,
                _actor_name(call.from_user),
                movement_type="WRITE_OFF",
                note="Списано зі складу без продажу",
            )
        except ValueError as exc:
            bot.answer_callback_query(call.id, str(exc), show_alert=True)
            return
        _edit_or_send(
            bot,
            call,
            _variant_text(variant) + "\n\nСписано без продажу. Оберт не змінено ✓",
            _variant_keyboard(variant, str(variant.get("movement_id") or "")),
        )
        bot.answer_callback_query(call.id, f"На складі: {variant['on_hand']}")
        return
    if action == "set" and len(parts) == 4:
        variant = get_inventory_variant(parts[3])
        if not variant or not int(variant.get("active") or 0):
            bot.answer_callback_query(
                call.id, "Колір або розмір не знайдено", show_alert=True
            )
            return
        save_inventory_wizard(
            user_id,
            chat_id,
            "SET_QUANTITY",
            {"variant_id": variant["id"]},
        )
        _edit_or_send(
            bot,
            call,
            _variant_text(variant) + "\n\nНапиши точну кількість одним числом.",
            _cancel_keyboard(),
        )
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id, "Невідома дія", show_alert=True)


def handle_inventory_text(bot: Any, message: Any) -> bool:
    user_id = int(message.from_user.id)
    wizard = get_inventory_wizard(user_id)
    if not wizard:
        return False
    text = (getattr(message, "text", None) or "").strip()
    if not text:
        bot.send_message(
            message.chat.id, "Надішли текстове значення або натисни «Скасувати»."
        )
        return True

    if wizard["step"] == "NEW_NAME":
        if len(text) < 2 or len(text) > 200:
            bot.send_message(
                message.chat.id, "Назва має містити від 2 до 200 символів."
            )
            return True
        payload = {"name": text, "sizes": [], "colors": list(COLOR_LABELS)}
        save_inventory_wizard(user_id, message.chat.id, "NEW_OPTIONS", payload)
        bot.send_message(
            message.chat.id,
            _options_text(payload),
            reply_markup=_options_keyboard(payload, "new"),
        )
        return True

    if wizard["step"] == "RENAME":
        if len(text) < 2 or len(text) > 200:
            bot.send_message(
                message.chat.id, "Назва має містити від 2 до 200 символів."
            )
            return True
        product = rename_product(wizard["payload"]["product_id"], text)
        if not product:
            bot.send_message(
                message.chat.id, "Не вдалося змінити назву. Можливо, вона вже зайнята."
            )
            return True
        clear_inventory_wizard(user_id)
        bot.send_message(
            message.chat.id,
            f"✅ Нова назва: {product['canonical_name']}",
            reply_markup=_edit_product_keyboard(product["id"]),
        )
        return True

    if wizard["step"] in {"NEW_COST_SEA", "NEW_COST_AIR"}:
        product_id = wizard["payload"]["product_id"]
        mode = "SEA" if wizard["step"] == "NEW_COST_SEA" else "AIR"
        try:
            profile = set_product_transport_cost(
                product_id,
                mode,
                text,
                user_id,
                _actor_name(message.from_user),
            )
        except ValueError as exc:
            bot.send_message(
                message.chat.id,
                f"{exc}. Напиши суму, наприклад 420 або 420,50.",
            )
            return True

        if mode == "SEA":
            save_inventory_wizard(
                user_id,
                message.chat.id,
                "NEW_COST_AIR",
                {"product_id": product_id},
            )
            bot.send_message(
                message.chat.id,
                (
                    f"Море: {format_money(profile['unit_cost_cents'])} грн/шт. ✓\n\n"
                    "Крок 2 із 2 — ✈️ доставка літаком\n"
                    "Тепер напиши собівартість 1 шт. разом із доставкою.\n\n"
                    "Наприклад: 600 або 600,50"
                ),
                reply_markup=_cancel_keyboard(),
            )
            return True

        clear_inventory_wizard(user_id)
        bot.send_message(
            message.chat.id,
            _inventory_product_text(product_id)
            + "\n\nОбидві собівартості збережено. Товар готовий ✓",
            reply_markup=_product_detail_keyboard(product_id),
        )
        return True

    if wizard["step"] in {"SET_PRODUCT_COST", "SET_RECEIPT_COST"}:
        payload = wizard["payload"]
        mode = payload["transport_mode"]
        if wizard["step"] == "SET_RECEIPT_COST":
            variant = get_inventory_variant(payload["variant_id"])
            if not variant:
                clear_inventory_wizard(user_id)
                bot.send_message(
                    message.chat.id, "Цього кольору або розміру вже не існує."
                )
                return True
            product_id = variant["product_id"]
        else:
            variant = None
            product_id = payload["product_id"]
        try:
            profile = set_product_transport_cost(
                product_id,
                mode,
                text,
                user_id,
                _actor_name(message.from_user),
            )
        except ValueError as exc:
            bot.send_message(
                message.chat.id,
                f"{exc}. Напиши суму, наприклад 420 або 420,50.",
            )
            return True
        clear_inventory_wizard(user_id)
        if variant:
            save_inventory_wizard(
                user_id, message.chat.id, "RECEIPT_QUANTITY",
                {"variant_id": variant['id'], "transport_mode": mode},
            )
            bot.send_message(
                message.chat.id,
                (
                    f"Ціну збережено: {format_money(profile['unit_cost_cents'])} грн/шт. ✓\n\n"
                    + _receipt_quantity_text(variant, mode)
                ),
                reply_markup=_receipt_quantity_keyboard(variant, mode),
            )
        else:
            bot.send_message(
                message.chat.id,
                _costs_text(product_id),
                reply_markup=_costs_keyboard(product_id),
            )
        return True

    if wizard["step"] == "RECEIPT_QUANTITY":
        if not re.fullmatch(r"[1-9]\d{0,5}", text):
            bot.send_message(message.chat.id, "Напиши ціле число від 1 до 999999.")
            return True
        payload = wizard["payload"]
        try:
            result = receive_inventory_batch(
                payload["variant_id"],
                payload["transport_mode"],
                int(text),
                user_id,
                _actor_name(message.from_user),
            )
        except ValueError as exc:
            bot.send_message(message.chat.id, str(exc))
            return True
        clear_inventory_wizard(user_id)
        bot.send_message(
            message.chat.id,
            _receipt_result_text(result),
            reply_markup=_receipt_result_keyboard(result),
        )
        return True

    if wizard["step"] == "WRITE_OFF_QUANTITY":
        if not re.fullmatch(r"[1-9]\d{0,5}", text):
            bot.send_message(message.chat.id, "Напиши ціле число від 1 до 999999.")
            return True
        quantity = int(text)
        try:
            variant = change_inventory_stock(
                wizard["payload"]["variant_id"],
                -quantity,
                user_id,
                _actor_name(message.from_user),
                movement_type="WRITE_OFF",
                note="Списано зі складу без продажу",
            )
        except ValueError as exc:
            bot.send_message(message.chat.id, str(exc))
            return True
        clear_inventory_wizard(user_id)
        bot.send_message(
            message.chat.id,
            (
                f"Списано: {quantity} шт. ✓\n"
                "Це не продаж — оберт не змінено.\n\n" + _variant_text(variant)
            ),
            reply_markup=_variant_keyboard(
                variant, str(variant.get("movement_id") or "")
            ),
        )
        return True

    if wizard["step"] == "SET_QUANTITY":
        if not re.fullmatch(r"\d{1,6}", text):
            bot.send_message(message.chat.id, "Напиши ціле число від 0 до 999999.")
            return True
        try:
            variant = set_inventory_stock(
                wizard["payload"]["variant_id"],
                int(text),
                user_id,
                _actor_name(message.from_user),
            )
        except ValueError as exc:
            bot.send_message(message.chat.id, str(exc))
            return True
        clear_inventory_wizard(user_id)
        bot.send_message(
            message.chat.id,
            "✅ Залишок оновлено.\n\n" + _variant_text(variant),
            reply_markup=_variant_keyboard(
                variant, str(variant.get("movement_id") or "")
            ),
        )
        return True
    return False
