"""Telegram button actions for drafts, 1C and returns."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from auth import ANALYTICS_ACCESS
from business_db import (
    COLOR_LABELS,
    clear_current_month_analytics,
    format_money,
    get_order_by_ttn,
    get_submission,
    mark_order_in_1c,
    mark_order_shipped_by_ttn,
    reserve_order_by_ttn,
    review_submission,
    update_order_status,
    update_order_status_by_ttn,
)
from config import (
    TG_ALERTS_THREAD_ID,
    TG_GROUP_ID,
    TIMEZONE,
    is_admin,
    is_employee,
    is_owner,
)
from inventory import handle_inventory_callback
from ui import awaiting_shipment_keyboard

LOGGER = logging.getLogger(__name__)


def _stamp() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y %H:%M")


def _actor(call: Any) -> tuple[int, str]:
    user = call.from_user
    return int(user.id), str(
        getattr(user, "full_name", None) or getattr(user, "username", "") or ""
    )


def _finish_message(bot: Any, call: Any, suffix: str) -> None:
    text = (getattr(call.message, "text", None) or "").rstrip()
    if suffix not in text:
        text = f"{text}\n\n{suffix}".strip()
    bot.edit_message_text(
        text=text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=None,
    )


def _update_submission_message(
    bot: Any,
    call: Any,
    suffix: str,
    reply_markup: Any,
) -> None:
    text = (getattr(call.message, "text", None) or "").rstrip()
    if suffix not in text:
        text = f"{text}\n\n{suffix}".strip()
    bot.edit_message_text(
        text=text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=reply_markup,
    )


def _process_pending_submission(
    bot: Any,
    submission: dict[str, Any],
    actor_id: int,
) -> bool:
    from submission_service import prepare_submission
    return prepare_submission(bot, submission, actor_id)[0]


def _inventory_result_text(result: dict[str, Any]) -> str:
    if result.get("items"):
        return "\n".join("%s %s %s: −%s шт., залишок %s" % (item["product_name"], item["size_code"], COLOR_LABELS.get(item["color_code"], item["color_code"]), item.get("deducted_quantity", 0), item.get("balance_after", 0)) for item in result["items"])
    status = result.get("inventory_status")
    if status == "already_recorded":
        return "Склад: повторно не списувався"
    if status == "unmatched":
        return "Склад: не списано, потрібна перевірка"
    color = COLOR_LABELS.get(str(result.get("color_code") or ""), "")
    variant = " ".join(
        part for part in (color, str(result.get("size_code") or "")) if part
    )
    deducted = int(result.get("deducted_quantity") or 0)
    balance = int(result.get("balance_after") or 0)
    return f"Склад: −{deducted} шт. ({variant}), залишок {balance}"


def _profit_result_text(result: dict[str, Any]) -> str:
    if result.get("profit_ready"):
        return "\n".join(
            [
                f"Собівартість: {format_money(result.get('purchase_cost_cents'))} грн",
                f"Прибуток: {format_money(result.get('profit_cents'))} грн",
            ]
        )
    if int(result.get("sale_price_cents") or 0) <= 0:
        return "Прибуток: ще не пораховано — немає ціни продажу"
    return "Прибуток: ще не пораховано — перевір собівартість"


def _send_inventory_alert(bot: Any, result: dict[str, Any]) -> None:
    warning = str(result.get("warning") or "").strip()
    if not warning:
        return
    color = COLOR_LABELS.get(str(result.get("color_code") or ""), "")
    variant = " ".join(
        part for part in (color, str(result.get("size_code") or "")) if part
    )
    lines = [
        "⚠️ Потрібна увага",
        f"Товар: {result.get('product_name') or 'Невідомий товар'}",
    ]
    if variant:
        lines.append(f"Колір і розмір: {variant}")
    if result.get("ttn"):
        lines.append(f"ТТН: {result['ttn']}")
    lines.append(f"Причина: {warning}")
    lines.append("Замовлення в 1С позначено. Перевір залишок через /stock.")
    try:
        bot.send_message(
            chat_id=TG_GROUP_ID,
            text="\n".join(lines),
            message_thread_id=TG_ALERTS_THREAD_ID,
        )
    except Exception:
        LOGGER.exception(
            "Could not send inventory warning for order %s", result.get("ttn")
        )


def _submission_action(bot: Any, call: Any, action: str, submission_id: str) -> None:
    actor_id, actor_name = _actor(call)
    submission = get_submission(submission_id)
    if not submission:
        bot.answer_callback_query(call.id, "Чернетку не знайдено", show_alert=True)
        return
    if action == "r":
        from repliq_inbox import shipment_allowed

        if not shipment_allowed(bot, call, submission_id):
            return
        if bool((submission.get("parsed") or {}).get("offline")):
            bot.answer_callback_query(
                call.id, "Для офлайн-продажу ця дія не потрібна", show_alert=True
            )
            return
        try:
            if not _process_pending_submission(bot, submission, actor_id):
                bot.answer_callback_query(
                    call.id, "Це замовлення вже обробляють або воно закрите",
                    show_alert=True,
                )
                return
        except Exception:
            bot.answer_callback_query(
                call.id, "Помилка. Можна натиснути ще раз.", show_alert=True
            )
            return
        reservation_warning = False
        for ttn in submission.get("ttns") or []:
            reservation = reserve_order_by_ttn(ttn, actor_id, actor_name)
            reservation_warning = bool(
                reservation_warning
                or not reservation
                or reservation.get("warning")
                or reservation.get("reservation_status")
                in {"shortage", "unmatched", "partial", "not_allowed"}
            )
        stock_note = (
            "Підтверджено ✓\n⚠️ Резерв складу потребує перевірки"
            if reservation_warning
            else "Підтверджено ✓\n📦 Товар зарезервовано й він очікує відправлення"
        )
        _update_submission_message(
            bot,
            call,
            f"{stock_note}\n👤 {actor_name or actor_id}\n🕒 {_stamp()}",
            awaiting_shipment_keyboard(submission_id),
        )
        from repliq_inbox import submission_confirmed

        submission_confirmed(submission_id)
        bot.answer_callback_query(
            call.id,
            "Замовлення підтверджено, але перевір склад"
            if reservation_warning
            else "Підтверджено — товар зарезервовано",
        )
        return
    if action == "c":
        from repliq_inbox import shipment_allowed

        if not shipment_allowed(bot, call, submission_id):
            return
        offline = bool((submission.get("parsed") or {}).get("offline"))
        if submission.get("status") in {"PENDING", "PROCESSING"}:
            try:
                if not _process_pending_submission(bot, submission, actor_id):
                    bot.answer_callback_query(
                        call.id, "Цю чернетку вже обробляють або оброблено"
                    )
                    return
            except Exception:
                bot.answer_callback_query(
                    call.id, "Помилка. Можна натиснути ще раз.", show_alert=True
                )
                return
        elif submission.get("status") != "CONFIRMED":
            bot.answer_callback_query(call.id, "Замовлення вже закрите")
            return

        reservation_warning = False
        if not offline:
            for ttn in submission.get("ttns") or []:
                shipment = mark_order_shipped_by_ttn(ttn, actor_id, actor_name)
                reservation_warning = bool(
                    reservation_warning
                    or not shipment
                    or (shipment and shipment.get("warning"))
                )
        label = "Продаж завершено ✓" if offline else "Відправлено ✓"
        from repliq_inbox import submission_finished

        submission_finished(submission_id)
        stock_note = (
            "\n⚠️ Резерв складу потребує перевірки"
            if reservation_warning
            else "\n📦 Товар перенесено у «В дорозі»"
            if not offline
            else ""
        )
        _finish_message(
            bot,
            call,
            f"{label}{stock_note}\n👤 {actor_name or actor_id}\n🕒 {_stamp()}",
        )
        bot.answer_callback_query(
            call.id,
            "Продаж завершено"
            if offline
            else "Відправлено, але перевір склад"
            if reservation_warning
            else "Відправлено — товар тепер у дорозі",
        )
        return
    if action == "x":
        from repliq_inbox import cancellation_allowed
        if not cancellation_allowed(bot, call, submission_id):
            return
        shipped = [
            order
            for ttn in submission.get("ttns") or []
            if (order := get_order_by_ttn(ttn))
            and order.get("status") not in {"CONFIRMED", "CANCELLED"}
        ]
        if shipped:
            bot.answer_callback_query(
                call.id,
                "Посилку вже відправлено — скасування тут недоступне",
                show_alert=True,
            )
            return
        if not review_submission(submission_id, "CANCELLED", actor_id):
            bot.answer_callback_query(call.id, "Чернетку вже оброблено")
            return
        for ttn in submission.get("ttns") or []:
            if get_order_by_ttn(ttn):
                update_order_status_by_ttn(
                    ttn,
                    "CANCELLED",
                    actor_user_id=actor_id,
                    actor_name=actor_name,
                )
        _finish_message(bot, call, f"Скасовано — {actor_name or actor_id}")
        from repliq_inbox import submission_finished
        submission_finished(submission_id, cancelled=True)
        bot.answer_callback_query(call.id, "Замовлення скасовано")
        return
    if action == "e":
        if not review_submission(submission_id, "NEEDS_EDIT", actor_id):
            bot.answer_callback_query(call.id, "Чернетку вже оброблено")
            return
        try:
            bot.send_message(
                submission["source_user_id"],
                "Замовлення потребує невеликого виправлення. Надішли виправлені дані новим повідомленням.",
            )
        except Exception:
            LOGGER.warning(
                "Could not notify draft owner %s", submission["source_user_id"]
            )
        _finish_message(bot, call, f"На виправлення — {actor_name or actor_id}")
        bot.answer_callback_query(call.id, "Клієнту надіслано прохання виправити")


def _order_action(bot: Any, call: Any, action: str, order_id: str) -> None:
    actor_id, actor_name = _actor(call)
    if action == "1c":
        result = mark_order_in_1c(order_id, actor_id, actor_name)
        if not result:
            bot.answer_callback_query(
                call.id, "Замовлення не знайдено", show_alert=True
            )
            return
        _send_inventory_alert(bot, result)
        _finish_message(
            bot,
            call,
            (
                f"Внесено в 1С ✓\n{_inventory_result_text(result)}\n"
                f"{_profit_result_text(result)}\n"
                f"👤 {actor_name or actor_id}\n🕒 {_stamp()}"
            ),
        )
        if result.get("warning"):
            answer = "Внесено в 1С. Склад перевір у гілці помилок"
        elif result.get("already_recorded"):
            answer = "Це замовлення вже було внесене в 1С"
        else:
            answer = "Внесено в 1С і списано зі складу"
        bot.answer_callback_query(call.id, answer)
        return
    status_map = {"ret": "RETURNED", "re": "RESHIP_REQUIRED", "close": "CLOSED"}
    status = status_map.get(action)
    if not status:
        bot.answer_callback_query(call.id, "Невідома дія", show_alert=True)
        return
    order = update_order_status(order_id, status, actor_id, actor_name)
    if not order:
        bot.answer_callback_query(call.id, "Замовлення не знайдено", show_alert=True)
        return
    labels = {
        "RETURNED": "📦 Товар повернувся",
        "RESHIP_REQUIRED": "🚚 Потрібно відправити повторно",
        "CLOSED": "✅ Повернення закрито",
    }
    _finish_message(bot, call, f"{labels[status]} — {actor_name or actor_id}")
    bot.answer_callback_query(call.id, labels[status])


def handle_callback(bot: Any, call: Any) -> None:
    from repliq_inbox import callback_payment
    if callback_payment(bot, call):
        return
    actor_id = int(call.from_user.id)
    data = str(call.data or "")
    if data.startswith('new:'):
        if not is_admin(actor_id):
            bot.answer_callback_query(call.id, 'Лише для адміністратора', show_alert=True)
            return
        from order_entry import handle_callback as handle_order_callback
        handle_order_callback(bot, call)
        return
    if data.startswith("analytics:"):
        if not is_owner(actor_id):
            bot.answer_callback_query(call.id, "Лише для власника", show_alert=True)
            return
        if not ANALYTICS_ACCESS.is_unlocked(actor_id):
            bot.answer_callback_query(
                call.id,
                "Фінанси вже закриті. Спочатку /unlock пароль",
                show_alert=True,
            )
            return
        if data.startswith('analytics:chart:'):
            if call.message.chat.id != actor_id or getattr(call.message.chat, 'type', 'private') != 'private':
                bot.answer_callback_query(call.id, 'Діаграми доступні в особистому чаті', show_alert=True)
                return
            period = data.split(':', 2)[2]
            if period not in ('month', 'all'):
                bot.answer_callback_query(call.id, 'Невідомий період', show_alert=True)
                return
            bot.answer_callback_query(call.id, 'Готую діаграми…')
            from handlers import _handle_charts
            _handle_charts(bot, actor_id, [period])
            return
        if data == "analytics:cancel:current":
            _finish_message(bot, call, "Очищення скасовано")
            bot.answer_callback_query(call.id, "Скасовано")
            return
        if data == "analytics:clear:current":
            actor_name = _actor(call)[1]
            result = clear_current_month_analytics(actor_id, actor_name)
            _finish_message(
                bot,
                call,
                f"Аналітику за {result['month']} очищено ✓\n🕒 {_stamp()}",
            )
            bot.answer_callback_query(call.id, "Поточний місяць очищено")
            return
        bot.answer_callback_query(call.id, "Некоректна дія", show_alert=True)
        return
    if data.startswith("inv:"):
        if not is_admin(actor_id):
            bot.answer_callback_query(
                call.id, "Лише для адміністратора", show_alert=True
            )
            return
        handle_inventory_callback(bot, call)
        return
    if not is_employee(actor_id):
        bot.answer_callback_query(call.id, "Немає доступу", show_alert=True)
        return
    try:
        entity, action, object_id = data.split(":", 2)
    except ValueError:
        bot.answer_callback_query(call.id, "Некоректна дія", show_alert=True)
        return
    if entity == "sub":
        _submission_action(bot, call, action, object_id)
    elif entity == "ord":
        _order_action(bot, call, action, object_id)
    else:
        bot.answer_callback_query(call.id, "Невідома дія", show_alert=True)
