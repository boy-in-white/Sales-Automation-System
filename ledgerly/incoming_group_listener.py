"""Consume source-topic messages and route them into tracking/1C topics."""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from config import (
    PRIVATE_FORWARD_TAG,
    SPECIAL_CLIENTS,
    TG_1C_THREAD_ID,
    TG_EMPLOYEES_THREAD_ID,
    TG_GROUP_ID,
    TG_SOURCE_THREAD_IDS,
    is_employee,
)
from parser import extract_ttns, parse_message
from business_db import (
    claim_notification,
    finish_notification,
    get_order_by_ttn,
    record_order,
)
from tracking_db import (
    claim_group_message,
    complete_group_message,
    release_group_message,
    update_shipment,
    upsert_shipment,
)
from tracking_notify import build_1c_text
from tracking_service import process_manual_ttn, ensure_tracking_message
from ukrposhta_api import normalize_ukr_ttn, configured as ukr_tracking_configured
from ui import onec_keyboard


LOGGER = logging.getLogger(__name__)
UKR_CANDIDATE_RE = re.compile(r"(?<![A-ZА-Я0-9])(?:\d{13}|[A-Z]{2}\d{9}[A-Z]{2})(?![A-ZА-Я0-9])", re.I)


def extract_ukr_candidates(text: str) -> list[str]:
    result: list[str] = []
    for token in UKR_CANDIDATE_RE.findall(text or ""):
        candidate = normalize_ukr_ttn(token)
        if candidate:result.append(candidate)
    return list(dict.fromkeys(result))


def source_text(message: Any) -> str:
    return str(getattr(message, "text", None) or getattr(message, "caption", None) or "")


def cleaned_source_text(message: Any) -> str:
    raw = source_text(message)
    if getattr(message, "message_thread_id", None) == TG_EMPLOYEES_THREAD_ID:
        raw = raw.replace(PRIVATE_FORWARD_TAG, "", 1).strip()
    return raw


def detect_carrier(text: str) -> str:
    low = (text or "").lower()
    if any(key in low for key in ("укрпошта", "укр пошта", "ukrposhta", "ukr post", "укр пошт")):
        return "ukrposhta"
    if (
        any(key in low for key in ("нова пошта", "novaposhta"))
        or re.search(r"\b(?:нп|np)\b", low)
    ):
        return "nova"
    if UKR_CANDIDATE_RE.search(text or ''):
        return "ukrposhta"
    return "unknown"


def _message_user_id(message: Any) -> int:
    forwarded = extract_uid_from_text(source_text(message))
    return forwarded or int(getattr(getattr(message, "from_user", None), "id", 0) or 0)


def _message_user_name(message: Any) -> str:
    forwarded = extract_client_name_from_text(source_text(message))
    if forwarded:
        return forwarded
    user = getattr(message, "from_user", None)
    return str(getattr(user, "full_name", None) or getattr(user, "username", None) or "")


def resolve_owner_name(message: Any) -> str | None:
    user_id = _message_user_id(message)
    special = SPECIAL_CLIENTS.get(user_id, {})
    return special.get("display_name") or extract_client_name_from_text(source_text(message))


def should_process(message: Any) -> bool:
    if int(message.chat.id) != TG_GROUP_ID:
        return False
    thread_id = getattr(message, "message_thread_id", None)
    # The "To ship" topic contains bot-created cards.  A human message there
    # must never bypass the per-card confirmation button.  Direct group input
    # is accepted only from explicitly configured source topics and employees.
    if thread_id not in TG_SOURCE_THREAD_IDS:
        return False
    user = getattr(message, "from_user", None)
    if bool(getattr(user, "is_bot", False)):
        return False
    return is_employee(int(getattr(user, "id", 0) or 0))


def send_temp_status(bot: Any, chat_id: int, thread_id: int | None, text: str, delay: float = 1.0) -> None:
    try:
        message = bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread_id)
    except Exception:
        LOGGER.debug("Could not send temporary status", exc_info=True)
        return

    def delete_message() -> None:
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            LOGGER.debug("Could not delete temporary status", exc_info=True)

    timer = threading.Timer(delay, delete_message)
    timer.daemon = True
    timer.start()


def extract_client_name_from_text(text: str) -> str | None:
    for line in (text or "").splitlines():
        if line.strip().lower().startswith("👤 клієнт:"):
            return line.split(":", 1)[1].strip()[:100] or None
    return None


def extract_uid_from_text(text: str) -> int | None:
    match = re.search(r"\bUID\s*:\s*(\d+)\b", text or "", re.I)
    return int(match.group(1)) if match else None


def make_offline_key(message: Any) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"OFFLINE-{stamp}-{getattr(message, 'message_id', 0)}"


def _record_offline(
    message: Any,
    parsed: dict[str, str],
    raw: str,
    owner_name: str | None,
    offline_key: str | None = None,
) -> str:
    now = int(time.time())
    key = offline_key or make_offline_key(message)
    shipment = upsert_shipment(
        ttn=key,
        account_name=owner_name,
        shipment_kind=parsed.get("shipment_kind") or "Офлайн",
        description=parsed.get("description") or "",
        cost=parsed.get("cost") or "",
        cost_text=parsed.get("cost_text") or "",
        olx_order_id=parsed.get("olx_order_id") or "",
        source_text=raw,
        source_user_id=_message_user_id(message),
        source_user_name=_message_user_name(message),
        source_chat_id=message.chat.id,
        source_thread_id=getattr(message, "message_thread_id", 0),
        source_message_id=message.message_id,
    )
    update_shipment(
        shipment["ttn"],
        last_status="Отримано",
        received_at=now,
        onec_sent_at=now,
        active=0,
        next_check_at=0,
    )
    return key


def _record_business_order(
    message: Any,
    parsed: dict[str, str],
    ttn: str,
    carrier: str,
    initial_status: str = "CONFIRMED",
) -> dict[str, Any]:
    return record_order(
        ttn=ttn,
        sale_type_label=parsed.get("shipment_kind") or "Наложка",
        carrier=carrier,
        product_name=parsed.get("product_name") or parsed.get("description") or "Невідомий товар",
        quantity=int(parsed.get("quantity") or 1),
        sale_price=parsed.get("sale_price") or parsed.get("cost"),
        purchase_cost=parsed.get("purchase_cost"),
        delivery_cost=parsed.get("delivery_cost"),
        platform_fee=parsed.get("platform_fee"),
        other_cost=parsed.get("other_cost"),
        olx_order_id=parsed.get("olx_order_id") or "",
        source_user_id=_message_user_id(message),
        source_user_name=_message_user_name(message),
        initial_status=initial_status,
    )


def _process_offline(bot: Any, message: Any, parsed: dict[str, str], raw: str, owner_name: str | None) -> None:
    now = int(time.time())
    offline_key = make_offline_key(message)
    order = _record_business_order(message, parsed, offline_key, "Offline", "DELIVERED")
    text = build_1c_text(
        shipment_kind=parsed.get("shipment_kind") or "Офлайн",
        account_name=owner_name,
        description=parsed.get("description") or "",
        cost=parsed.get("cost") or "",
        cost_text=parsed.get("cost_text") or "",
        olx_order_id=parsed.get("olx_order_id") or "",
        ttn="",
        status="Отримано",
        place=parsed.get("city") or "",
        received_at=now,
        source_label="Офлайн",
    )
    if claim_notification(order["id"], "1C"):
        try:
            sent = bot.send_message(
                chat_id=TG_GROUP_ID,
                text=text,
                message_thread_id=TG_1C_THREAD_ID,
                reply_markup=onec_keyboard(order["id"]),
            )
            finish_notification(order["id"], "1C", True, sent.message_id)
        except Exception:
            finish_notification(order["id"], "1C", False)
            raise
    _record_offline(message, parsed, raw, owner_name, offline_key)


def _record_ukrposhta(
    bot: Any,
    message: Any,
    parsed: dict[str, str],
    raw: str,
    owner_name: str | None,
    ttn: str,
) -> None:
    shipment = upsert_shipment(
        ttn=ttn,
        account_name=owner_name,
        shipment_kind=parsed.get("shipment_kind") or "Наложка",
        description=parsed.get("description") or "",
        cost=parsed.get("cost") or "",
        cost_text=parsed.get("cost_text") or "",
        olx_order_id=parsed.get("olx_order_id") or "",
        source_text=raw,
        source_user_id=_message_user_id(message),
        source_user_name=_message_user_name(message),
        source_chat_id=message.chat.id,
        source_thread_id=getattr(message, "message_thread_id", 0),
        source_message_id=message.message_id,
    )
    _record_business_order(message, parsed, ttn, "Ukrposhta")
    order=get_order_by_ttn(ttn)
    if order and order.get('status') in ('DELIVERED','CANCELLED','RETURNED','CLOSED'):
        return
    if not shipment.get('last_status') or shipment['last_status']=='Ручне відстеження':
        label='Очікує даних трекінгу' if ukr_tracking_configured() else 'Очікує підключення трекінгу'
        update_shipment(ttn,last_status=label,active=1,next_check_at=0)
        snapshot={'status_label':label,'status_code':'','terminal':False,'place':'','doc':{},'profile_name':'ukrposhta:parsebot'}
        ensure_tracking_message(bot,shipment,snapshot)


def _process_claimed_message(bot: Any, message: Any) -> str:
    raw = cleaned_source_text(message)
    if not raw:
        return "ignored"
    parsed = parse_message(raw)
    ttns = extract_ttns(raw)
    owner_name = resolve_owner_name(message)
    carrier = detect_carrier(raw)
    low = raw.lower()

    if "офлайн" in low or "offline" in low:
        _process_offline(bot, message, parsed, raw, owner_name)
        return "processed"

    if not ttns and carrier == "ukrposhta":
        ttns = extract_ukr_candidates(raw)
    if not ttns:
        return "ignored"

    if carrier == "ukrposhta":
        ttns=extract_ukr_candidates(raw)
        for ttn in ttns:
            _record_ukrposhta(bot,message,parsed,raw,owner_name,ttn)
        return "processed"

    processed = False
    repeated = False
    for ttn in ttns:
        _record_business_order(message, parsed, ttn, "Nova Poshta")
        ok, _snapshot, _changed, mode = process_manual_ttn(
            bot,
            ttn,
            raw,
            description=parsed.get("description"),
            shipment_kind=parsed.get("shipment_kind") or "Наложка",
            cost_text=parsed.get("cost_text") or "",
            owner_name=owner_name,
            source_user_id=_message_user_id(message),
            source_user_name=_message_user_name(message),
            source_chat_id=message.chat.id,
            source_thread_id=getattr(message, "message_thread_id", 0),
            source_message_id=message.message_id,
            olx_order_id=parsed.get("olx_order_id") or "",
        )
        repeated = repeated or mode == "duplicate"
        processed = processed or (ok and mode != "duplicate")
    if processed:
        return "processed"
    if repeated:
        return "repeat"
    return "failed"


def process_submission(bot: Any, submission: dict[str, Any]) -> str:
    """Create the order and tracking record after the owner confirms the order."""

    fake_user = SimpleNamespace(
        id=int(submission.get("source_user_id") or 0),
        full_name=submission.get("source_user_name") or "",
        username=None,
        is_bot=False,
    )
    fake_message = SimpleNamespace(
        text=submission.get("raw_text") or "",
        caption=None,
        message_thread_id=TG_EMPLOYEES_THREAD_ID,
        message_id=int(submission.get("telegram_message_id") or 0),
        chat=SimpleNamespace(id=TG_GROUP_ID),
        from_user=fake_user,
    )
    return _process_claimed_message(bot, fake_message)


def handle_incoming_group_message(bot: Any, message: Any) -> None:
    if not should_process(message):
        return
    thread_id = getattr(message, "message_thread_id", None)
    if not claim_group_message(message.chat.id, thread_id, message.message_id):
        LOGGER.debug("Skipping claimed/processed group message %s", message.message_id)
        return
    try:
        outcome = _process_claimed_message(bot, message)
        complete_group_message(message.chat.id, thread_id, message.message_id)
        if outcome == "processed":
            send_temp_status(bot, TG_GROUP_ID, thread_id, "☑️ Оброблено", delay=0.6)
        elif outcome == "repeat":
            send_temp_status(bot, TG_GROUP_ID, thread_id, "🔁 Повтор", delay=1.0)
        elif outcome == "failed":
            send_temp_status(bot, TG_GROUP_ID, thread_id, "⚠️ Не вдалося знайти ТТН", delay=2.0)
    except Exception as exc:
        release_group_message(
            message.chat.id,
            thread_id,
            message.message_id,
            type(exc).__name__,
        )
        LOGGER.exception("Failed to process group message %s", message.message_id)
        send_temp_status(bot, TG_GROUP_ID, thread_id, "⚠️ Помилка обробки", delay=2.0)

