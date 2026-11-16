"""Telegram inline keyboards. Callback payloads stay below Telegram's limit."""

from telebot import types


def submission_keyboard(
    submission_id: str,
    *,
    offline: bool = False,
) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "Продаж завершено ✓" if offline else "Підтвердити ✓",
            callback_data=f"sub:c:{submission_id}" if offline else f"sub:r:{submission_id}",
        ),
        types.InlineKeyboardButton("Виправити дані", callback_data=f"sub:e:{submission_id}"),
    )
    keyboard.add(
        types.InlineKeyboardButton("Скасувати", callback_data=f"sub:x:{submission_id}")
    )
    return keyboard


def awaiting_shipment_keyboard(submission_id: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "Позначити як відправлене ✓", callback_data=f"sub:c:{submission_id}"
        ),
        types.InlineKeyboardButton(
            "Скасувати", callback_data=f"sub:x:{submission_id}"
        ),
    )
    return keyboard


def onec_keyboard(order_id: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton("Готово в 1С ✓", callback_data=f"ord:1c:{order_id}")
    )
    return keyboard


def return_keyboard(order_id: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("Товар повернувся", callback_data=f"ord:ret:{order_id}"),
        types.InlineKeyboardButton("Відправити ще раз", callback_data=f"ord:re:{order_id}"),
    )
    keyboard.add(
        types.InlineKeyboardButton("Закрити ✓", callback_data=f"ord:close:{order_id}")
    )
    return keyboard


def clear_current_month_keyboard() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            "Так, очистити",
            callback_data="analytics:clear:current",
        ),
        types.InlineKeyboardButton(
            "Скасувати",
            callback_data="analytics:cancel:current",
        ),
    )
    return keyboard

