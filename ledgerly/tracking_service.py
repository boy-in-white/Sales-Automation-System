"""Read-only tracking orchestration and Telegram/1C-topic notifications."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from config import (
    ACCOUNTS,
    TG_1C_THREAD_ID,
    TG_GROUP_ID,
    TG_RETURNS_THREAD_ID,
    TG_TRACKING_THREAD_ID,
)
from business_db import (
    claim_notification,
    finish_notification,
    get_notification,
    get_order_by_ttn,
    record_order,
    update_order_status_by_ttn,
)
from nova_api import get_tracking_document
from parser import normalize_ttn_candidate, safe_eval_cost_expr
from tracking_db import (
    finish_1c_delivery,
    get_shipment,
    record_check_failure,
    record_check_success,
    release_tracking_claim,
    reserve_1c_delivery,
    reserve_tracking_message_slot,
    set_tracking_message_id,
    update_shipment,
    upsert_shipment,
)
from tracking_notify import build_1c_text, build_tracking_text, normalize_shipment_kind
from ui import onec_keyboard, return_keyboard
from ukrposhta_api import normalize_ukr_ttn


LOGGER = logging.getLogger(__name__)


def ordered_accounts(preferred: str | None = None) -> list[str]:
    names = [name for name, account in ACCOUNTS.items() if account.get("API_KEY")]
    if preferred in names:
        names = [preferred] + [name for name in names if name != preferred]
    result, seen = [], set()
    for name in names:
        if ACCOUNTS[name]["API_KEY"] not in seen:
            result.append(name)
            seen.add(ACCOUNTS[name]["API_KEY"])
    return result


def detect_shipment_kind(text: str | None, default: str = "Наложка") -> str:
    return normalize_shipment_kind(text, default=default)


def extract_doc(response: dict[str, Any]) -> dict[str, Any]:
    if not response.get("success", False):
        return {}
    data = response.get("data") or []
    return data[0] if data and isinstance(data[0], dict) else {}


def _document_not_found(doc: dict[str, Any]) -> bool:
    code = str(doc.get("StatusCode") or "").strip()
    text = " ".join(
        str(doc.get(key) or "")
        for key in ("Status", "StatusDescription", "StateName", "StateDescription")
    ).lower()
    return code == "3" or "not found" in text or "не знайден" in text


def classify_tracking_doc(doc: dict[str, Any]) -> tuple[str, bool, str]:
    raw_status = str(
        doc.get("Status")
        or doc.get("StatusDescription")
        or doc.get("StateName")
        or doc.get("StateDescription")
        or ""
    ).strip()
    raw_code = str(
        doc.get("StatusCode") or doc.get("StateId") or doc.get("StateCode") or ""
    ).strip()
    text = " ".join(
        [
            str(doc.get("Status") or ""),
            str(doc.get("StatusDescription") or ""),
            str(doc.get("StateName") or ""),
            str(doc.get("StateDescription") or ""),
            raw_code,
        ]
    ).lower()
    if any(word in text for word in ("відмов", "refus", "повернен", "return to sender")):
        return "Відмова", True, raw_code
    if any(word in text for word in ("отрим", "received", "delivered", "вруч")):
        return "Отримано", True, raw_code
    if any(phrase in text for phrase in ("прибув у відділення", "прибуло у відділення", "arrived at branch")):
        return "Прибув у відділення", False, raw_code
    if any(word in text for word in ("відправ", "in transit", "транзит", "дорозі", "shipped")):
        return "Відправлено", False, raw_code
    if any(word in text for word in ("очіку", "created", "створ", "pending", "зареєстр", "accepted")):
        return "Очікує відправки", False, raw_code
    return (raw_status or "Очікує відправки"), False, raw_code


def extract_place(doc: dict[str, Any]) -> str:
    for key in (
        "CurrentCity",
        "CityRecipient",
        "RecipientAddressDescription",
        "WarehouseRecipientDescription",
        "RecipientCityDescription",
        "SettlementDescription",
    ):
        value = doc.get(key)
        if value:
            return str(value).strip()
    return ""


def next_delay_seconds(status_label: str, terminal: bool) -> int:
    if terminal:
        return 24 * 60 * 60
    return {
        "Очікує відправки": 15 * 60,
        "Прибув у відділення": 30 * 60,
        "Відправлено": 30 * 60,
    }.get(status_label, 30 * 60)


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    stable_data = {
        "status_label": snapshot.get("status_label", ""),
        "status_code": snapshot.get("status_code", ""),
        "place": snapshot.get("place", ""),
        "actual_delivery": (snapshot.get("doc") or {}).get("ActualDeliveryDate", ""),
    }
    return hashlib.sha256(
        json.dumps(stable_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def fetch_tracking_snapshot(ttn: str, preferred_account: str | None = None) -> dict[str, Any] | None:
    if normalize_ukr_ttn(ttn):
        from ukrposhta_tracker import cached_snapshot
        return cached_snapshot(normalize_ukr_ttn(ttn))
    normalized = normalize_ttn_candidate(ttn)
    if not normalized:
        return None
    for profile_name in ordered_accounts(preferred_account):
        response = get_tracking_document(ACCOUNTS[profile_name]["API_KEY"], normalized)
        doc = extract_doc(response)
        if not doc or _document_not_found(doc):
            continue
        status_label, terminal, status_code = classify_tracking_doc(doc)
        snapshot = {
            "profile_name": profile_name,
            "doc": doc,
            "status_label": status_label,
            "terminal": terminal,
            "status_code": status_code,
            "place": extract_place(doc),
        }
        snapshot["hash"] = snapshot_hash(snapshot)
        return snapshot
    return None


def _tracking_text(shipment: dict[str, Any], snapshot: dict[str, Any], received_at: int | None) -> str:
    prefix='📮 Укрпошта\n' if normalize_ukr_ttn(shipment.get('ttn')) else ''
    return prefix+build_tracking_text(
        shipment_kind=shipment.get("shipment_kind") or "Наложка",
        account_name=shipment.get("account_name"),
        description=shipment.get("description"),
        cost=shipment.get("cost"),
        cost_text=shipment.get("cost_text") or "",
        olx_order_id=shipment.get("olx_order_id") or "",
        ttn=shipment.get("ttn") or "",
        status=snapshot["status_label"],
        place=snapshot.get("place", ""),
        received_at=received_at,
        source_label="Автотрекінг",
    )


def _send_tracking_message(bot: Any, shipment: dict[str, Any], text: str) -> tuple[int | None, str]:
    try:
        message = bot.send_message(
            chat_id=TG_GROUP_ID,
            text=text,
            message_thread_id=TG_TRACKING_THREAD_ID,
        )
        set_tracking_message_id(
            shipment["ttn"],
            message.message_id,
            chat_id=TG_GROUP_ID,
            thread_id=TG_TRACKING_THREAD_ID,
        )
        return int(message.message_id), "sent"
    except Exception as exc:
        LOGGER.exception("Could not send tracking card for %s", shipment.get("ttn"))
        release_tracking_claim(shipment["ttn"])
        return None, f"send_failed:{type(exc).__name__}"


def ensure_tracking_message(
    bot: Any,
    shipment: dict[str, Any],
    snapshot: dict[str, Any],
    received_at: int | None = None,
) -> tuple[int | None, str]:
    state, message_id = reserve_tracking_message_slot(shipment["ttn"])
    text = _tracking_text(shipment, snapshot, received_at)
    if state == "existing" and message_id:
        try:
            bot.edit_message_text(text=text, chat_id=TG_GROUP_ID, message_id=message_id)
            return message_id, "edited"
        except Exception as exc:
            error_text = str(exc).lower()
            if "message is not modified" in error_text:
                return message_id, "unchanged"
            LOGGER.warning(
                "Tracking card %s for %s could not be edited; recreating it",
                message_id,
                shipment["ttn"],
            )
            set_tracking_message_id(shipment["ttn"], 0)
            state, _ = reserve_tracking_message_slot(shipment["ttn"])
    if state == "claimed":
        return _send_tracking_message(bot, shipment, text)
    return None, state


def _send_to_1c(bot: Any, shipment: dict[str, Any], snapshot: dict[str, Any], received_at: int) -> bool:
    if not reserve_1c_delivery(shipment["ttn"]):
        current = get_shipment(shipment["ttn"]) or {}
        return int(current.get("onec_sent_at") or 0) > 0
    order = _ensure_business_order(shipment)
    if not claim_notification(order["id"], "1C"):
        notification = get_notification(order["id"], "1C") or {}
        sent = notification.get("status") == "SENT"
        finish_1c_delivery(shipment["ttn"], success=sent)
        return sent
    text = build_1c_text(
        shipment_kind=shipment.get("shipment_kind") or "Наложка",
        account_name=shipment.get("account_name"),
        description=shipment.get("description"),
        cost=shipment.get("cost"),
        cost_text=shipment.get("cost_text") or "",
        olx_order_id=shipment.get("olx_order_id") or "",
        ttn=shipment.get("ttn") or "",
        status=snapshot["status_label"],
        place=snapshot.get("place", ""),
        received_at=received_at,
        source_label="Автотрекінг",
    )
    if normalize_ukr_ttn(shipment.get('ttn')):
        text='📮 Укрпошта\n'+text
    try:
        message = bot.send_message(
            chat_id=TG_GROUP_ID,
            text=text,
            message_thread_id=TG_1C_THREAD_ID,
            reply_markup=onec_keyboard(order["id"]),
        )
    except Exception:
        finish_1c_delivery(shipment["ttn"], success=False)
        finish_notification(order["id"], "1C", False)
        LOGGER.exception("Could not deliver %s to the 1C topic", shipment["ttn"])
        return False
    finish_1c_delivery(shipment["ttn"], success=True)
    finish_notification(order["id"], "1C", True, message.message_id)
    return True


def _ensure_business_order(shipment: dict[str, Any]) -> dict[str, Any]:
    order = get_order_by_ttn(shipment["ttn"])
    if order:
        return order
    return record_order(
        ttn=shipment["ttn"],
        sale_type_label=shipment.get("shipment_kind") or "Наложка",
        carrier="Ukrposhta" if normalize_ukr_ttn(shipment.get('ttn')) else "Nova Poshta",
        product_name=shipment.get("description") or "Невідомий товар",
        sale_price=shipment.get("cost") or shipment.get("cost_text"),
        olx_order_id=shipment.get("olx_order_id") or "",
        source_user_id=shipment.get("source_user_id") or 0,
        source_user_name=shipment.get("source_user_name") or "",
    )


def _send_return_notice(bot: Any, shipment: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    order = _ensure_business_order(shipment)
    if not claim_notification(order["id"], "RETURN"):
        notification = get_notification(order["id"], "RETURN") or {}
        return notification.get("status") == "SENT"
    text = build_1c_text(
        shipment_kind=shipment.get("shipment_kind") or "Наложка",
        account_name=shipment.get("account_name"),
        description=shipment.get("description"),
        cost=shipment.get("cost"),
        cost_text=shipment.get("cost_text") or "",
        olx_order_id=shipment.get("olx_order_id") or "",
        ttn=shipment.get("ttn") or "",
        status="Відмова",
        place=snapshot.get("place", ""),
        source_label="Повернення",
    )
    try:
        message = bot.send_message(
            chat_id=TG_GROUP_ID,
            text=text,
            message_thread_id=TG_RETURNS_THREAD_ID,
            reply_markup=return_keyboard(order["id"]),
        )
    except Exception:
        finish_notification(order["id"], "RETURN", False)
        LOGGER.exception("Could not send return notice for %s", shipment["ttn"])
        return False
    finish_notification(order["id"], "RETURN", True, message.message_id)
    return True


def apply_snapshot(
    bot: Any,
    shipment: dict[str, Any],
    snapshot: dict[str, Any],
    send_to_1c: bool = True,
) -> bool:
    now = int(time.time())
    event_at=int(snapshot.get('event_at') or 0)
    existing_order = get_order_by_ttn(shipment["ttn"])
    if existing_order and existing_order.get("status") in {
        "CANCELLED",
        "CLOSED",
        "RETURNED",
    }:
        update_shipment(shipment["ttn"], active=0, last_error="")
        return False
    if snapshot.get('carrier') == 'Ukrposhta':
        if event_at < int(shipment.get('created_at') or now) - 45 * 86400:
            update_shipment(shipment['ttn'], last_error='ukr_event_too_old_for_order')
            return False
    if event_at and event_at<int(shipment.get('last_event_at') or 0):
        return False
    received_at = int(shipment.get("received_at") or 0)
    if snapshot["status_label"] == "Отримано" and not received_at:
        received_at = event_at or now
    changed = any(
        (
            (shipment.get("last_status") or "") != snapshot["status_label"],
            (shipment.get("last_status_code") or "") != snapshot["status_code"],
            (shipment.get("status_hash") or "") != snapshot["hash"],
        )
    )
    update_shipment(
        shipment["ttn"],
        profile_name=snapshot["profile_name"],
        last_status=snapshot["status_label"],
        last_status_code=snapshot["status_code"],
        last_event_at=event_at or int(shipment.get('last_event_at') or 0),
        status_hash=snapshot["hash"],
        received_at=received_at,
        active=0 if snapshot["terminal"] else 1,
        next_check_at=now + int(snapshot.get('poll_seconds') or next_delay_seconds(snapshot["status_label"], snapshot["terminal"])),
        last_checked_at=now,
        consecutive_failures=0,
        last_error="",
    )
    order = _ensure_business_order(shipment)
    business_status = str(order.get("status") or "")
    required_notification_ready = True
    if snapshot["status_label"] == "Отримано":
        if business_status != "DELIVERED":
            update_order_status_by_ttn(shipment["ttn"], "DELIVERED")
    elif snapshot["status_label"] == "Відмова":
        if business_status != "REFUSED":
            update_order_status_by_ttn(shipment["ttn"], "REFUSED")
        required_notification_ready = _send_return_notice(bot, shipment, snapshot)
    elif snapshot["status_label"] in {"Відправлено", "Прибув у відділення"}:
        if business_status != "SHIPPED":
            update_order_status_by_ttn(shipment["ttn"], "SHIPPED")
    tracking_message_ready = int(shipment.get("tracking_message_id") or 0) > 0
    if changed or int(shipment.get("tracking_message_id") or 0) <= 0:
        message_id, _message_state = ensure_tracking_message(
            bot, shipment, snapshot, received_at or None
        )
        tracking_message_ready = bool(message_id)
    if snapshot["status_label"] == "Отримано" and send_to_1c:
        required_notification_ready = _send_to_1c(
            bot, shipment, snapshot, received_at or now
        )
    record_check_success(shipment["ttn"])
    if not (tracking_message_ready and required_notification_ready):
        # A failed Telegram card must not disappear just because the carrier
        # status was saved successfully.  Durable notification claims still
        # prevent duplicate cards during the retry.
        update_shipment(
            shipment["ttn"],
            active=1,
            next_check_at=now + 5 * 60,
            last_error="telegram_notification_pending",
        )
    elif snapshot["terminal"]:
        update_shipment(shipment["ttn"], active=0, last_error="")
    return changed


def _owner(owner_name: str | None, existing: dict[str, Any]) -> str:
    candidate = str(owner_name or "").strip()
    if candidate and candidate != "Unknown":
        return candidate
    return str(existing.get("account_name") or "").strip()


def _has_explicit_kind(text: str | None) -> bool:
    low = str(text or "").lower()
    return any(
        marker in low
        for marker in (
            "налож", "олх", "olx", "фоп", "офлайн", "offline",
        )
    )


def refresh_ttn(
    bot: Any,
    ttn: str,
    preferred_account: str | None = None,
    source_text: str | None = None,
    description: str | None = None,
    shipment_kind: str | None = None,
    cost_text: str | None = None,
    owner_name: str | None = None,
    source_user_id: int | None = None,
    source_user_name: str | None = None,
    source_chat_id: int | None = None,
    source_thread_id: int | None = None,
    source_message_id: int | None = None,
    olx_order_id: str | None = None,
    send_to_1c: bool = True,
) -> tuple[bool, dict[str, Any] | None, bool, str]:
    normalized = normalize_ukr_ttn(ttn) or normalize_ttn_candidate(ttn)
    if not normalized:
        return False, None, False, "invalid"
    existing = get_shipment(normalized) or {}
    snapshot = fetch_tracking_snapshot(normalized, preferred_account=preferred_account)
    if not snapshot:
        if normalize_ukr_ttn(normalized):
            return False, None, False, 'ukrposhta_scheduled'
        if existing:
            record_check_failure(normalized, "Tracking document was not found")
        return False, None, False, "not_found"
    final_description = (
        str(description or "").strip()
        or str(existing.get("description") or "").strip()
        or (str(source_text or "").splitlines()[0][:150] if source_text else "")
    )
    final_kind = normalize_shipment_kind(
        shipment_kind or existing.get("shipment_kind") or detect_shipment_kind(source_text)
    )
    raw_cost = str(cost_text or existing.get("cost_text") or "").strip()
    shipment = upsert_shipment(
        ttn=normalized,
        account_name=_owner(owner_name, existing),
        profile_name=snapshot["profile_name"],
        shipment_kind=final_kind,
        description=final_description,
        cost=safe_eval_cost_expr(raw_cost) or str(existing.get("cost") or ""),
        cost_text=raw_cost,
        olx_order_id=olx_order_id or existing.get("olx_order_id") or "",
        source_text=source_text or existing.get("source_text") or "",
        source_user_id=source_user_id,
        source_user_name=source_user_name,
        source_chat_id=source_chat_id,
        source_thread_id=source_thread_id,
        source_message_id=source_message_id,
    )
    changed = apply_snapshot(bot, shipment, snapshot, send_to_1c=send_to_1c)
    return True, snapshot, changed, "ok"


def process_manual_ttn(
    bot: Any,
    ttn: str,
    source_text: str,
    description: str | None = None,
    shipment_kind: str = "Наложка",
    cost_text: str = "",
    owner_name: str | None = None,
    source_user_id: int | None = None,
    source_user_name: str | None = None,
    source_chat_id: int | None = None,
    source_thread_id: int | None = None,
    source_message_id: int | None = None,
    olx_order_id: str | None = None,
) -> tuple[bool, dict[str, Any] | None, bool, str]:
    normalized = normalize_ttn_candidate(ttn)
    if not normalized:
        return False, None, False, "invalid"
    existing = get_shipment(normalized)
    if existing:
        upsert_shipment(
            ttn=normalized,
            account_name=owner_name,
            shipment_kind=shipment_kind if _has_explicit_kind(source_text) else None,
            description=description,
            cost=safe_eval_cost_expr(cost_text),
            cost_text=cost_text,
            olx_order_id=olx_order_id,
            source_text=source_text,
            source_user_id=source_user_id,
            source_user_name=source_user_name,
            source_chat_id=source_chat_id,
            source_thread_id=source_thread_id,
            source_message_id=source_message_id,
        )
        return True, {"status_label": existing.get("last_status") or "Вже існує"}, False, "duplicate"

    pending = upsert_shipment(
        ttn=normalized,
        account_name=owner_name,
        shipment_kind=shipment_kind,
        description=description,
        cost=safe_eval_cost_expr(cost_text),
        cost_text=cost_text,
        olx_order_id=olx_order_id,
        source_text=source_text,
        source_user_id=source_user_id,
        source_user_name=source_user_name,
        source_chat_id=source_chat_id,
        source_thread_id=source_thread_id,
        source_message_id=source_message_id,
    )
    result = refresh_ttn(
        bot,
        normalized,
        source_text=source_text,
        description=description,
        shipment_kind=shipment_kind,
        cost_text=cost_text,
        owner_name=owner_name,
        source_user_id=source_user_id,
        source_user_name=source_user_name,
        source_chat_id=source_chat_id,
        source_thread_id=source_thread_id,
        source_message_id=source_message_id,
        olx_order_id=olx_order_id,
        send_to_1c=True,
    )
    if result[0]:
        return result

    # A newly handed-over TTN may take a few minutes to appear in Nova Poshta.
    # Keep it active and show a tracking card instead of making the user press
    # the button repeatedly.
    pending = get_shipment(normalized) or pending
    waiting_snapshot = {
        "profile_name": pending.get("profile_name") or "",
        "doc": {},
        "status_label": "Очікує появи в Новій пошті",
        "terminal": False,
        "status_code": "",
        "place": "",
    }
    waiting_snapshot["hash"] = snapshot_hash(waiting_snapshot)
    ensure_tracking_message(bot, pending, waiting_snapshot)
    update_shipment(normalized, active=1)
    return True, waiting_snapshot, True, "queued"

