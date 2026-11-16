"""Plain-text Telegram cards for tracking and bookkeeping topics."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from config import TIMEZONE
from parser import normalize_ttn_candidate


def normalize_shipment_kind(kind: str | None, default: str = "Наложка") -> str:
    low = (kind or "").strip().lower()
    if "офлайн" in low or "offline" in low:
        return "Офлайн"
    if "фоп" in low:
        return "ФОП"
    if "налож" in low:
        return "Наложка"
    if "олх" in low or "olx" in low:
        return "ОЛХ"
    return default


def status_emoji(status: str) -> str:
    low = (status or "").strip().lower()
    if "отрим" in low or "вруч" in low:
        return "✅"
    if "відмов" in low:
        return "↩️"
    if "прибув" in low or "прибул" in low:
        return "📦"
    if "відправ" in low or "дорозі" in low:
        return "🚚"
    if "очіку" in low:
        return "⏳"
    return "ℹ️"


def format_ts(ts: int | str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError, OSError):
        return ""


def _header_icon(kind: str) -> str:
    return {
        "ФОП": "💼",
        "ОЛХ": "🛒",
        "Офлайн": "🏷️",
    }.get(kind, "📦")


def sanitize_description(description: str | None) -> str:
    result: list[str] = []
    for raw_line in (description or "").splitlines():
        line = raw_line.strip()
        low = line.lower()
        if not line or low in {
            "фоп", "наложка", "олх", "olx", "офлайн", "offline",
        }:
            continue
        if normalize_ttn_candidate(line):
            continue
        if re.match(r"^(?:uid|ттн|пошта|сума|вартість|ціна|cost|price)\s*:", low):
            continue
        # A price must never be rendered as a product description, even if an
        # older parser or imported record put it in the wrong field.
        amount = re.sub(r"\s*(?:грн\.?|uah|₴)\s*$", "", line, flags=re.I).strip()
        if re.fullmatch(r"[0-9][0-9\s+\-*/().,]*", amount):
            continue
        result.append(line)
    return "\n".join(result).strip()[:1200]


def _cost_display(cost: str | None = None, cost_text: str = "") -> list[str]:
    raw = (cost_text or "").strip()
    normalized = str(cost or "").strip()
    if normalized and raw and normalized != raw.replace(",", "."):
        return [f"💰 Сума: {normalized} грн", f"↳ Формула: {raw}"]
    if normalized:
        return [f"💰 Сума: {normalized} грн"]
    if raw:
        return [f"💰 Сума: {raw}"]
    return []


def _render_card(
    kind: str,
    owner: str | None,
    description: str | None,
    cost: str | None,
    cost_text: str,
    ttn: str,
    status: str,
    place: str = "",
    source_label: str = "",
    received_at: int | None = None,
    olx_order_id: str = "",
) -> str:
    kind = normalize_shipment_kind(kind)
    owner = (owner or "").strip()
    header = f"{_header_icon(kind)} «{kind}»"
    if owner and owner != "Unknown":
        header += f" {owner}"
    lines = [header, ""]
    desc = sanitize_description(description)
    if desc:
        lines.extend(["📝 Опис:", desc, ""])
    lines.extend(_cost_display(cost, cost_text))
    if olx_order_id:
        lines.append(f"🛒 OLX ID: {olx_order_id}")
    if ttn:
        lines.append(f"🔖 ТТН: {ttn}")
    lines.append(f"{status_emoji(status)} Статус: {status}")
    if place:
        lines.append(f"📍 Де: {str(place).strip()[:300]}")
    if received_at:
        lines.extend(["", f"🕒 Отримано: {format_ts(received_at)}"])
    if source_label:
        lines.extend(["", f"🧭 Джерело: {source_label}"])
    return "\n".join(lines).strip()[:4000]


def build_tracking_text(
    shipment_kind: str,
    account_name: str | None,
    description: str | None,
    cost: str | None,
    ttn: str,
    status: str,
    place: str = "",
    cost_text: str = "",
    received_at: int | None = None,
    source_label: str = "Автотрекінг",
    olx_order_id: str = "",
) -> str:
    return _render_card(
        shipment_kind, account_name, description, cost, cost_text, ttn, status,
        place, source_label, received_at, olx_order_id,
    )


def build_1c_text(
    shipment_kind: str,
    account_name: str | None,
    description: str | None,
    cost: str | None,
    ttn: str,
    status: str,
    place: str = "",
    cost_text: str = "",
    received_at: int | None = None,
    source_label: str = "Передано в 1С",
    olx_order_id: str = "",
) -> str:
    return _render_card(
        shipment_kind, account_name, description, cost, cost_text, ttn, status,
        place, source_label, received_at, olx_order_id,
    )


def build_employee_text(
    client_name: str | None,
    user_id: int,
    shipment_kind: str,
    description: str | None,
    cost_text: str,
    ttn: str = "",
    offline: bool = False,
    ttns: list[str] | None = None,
    olx_order_id: str = "",
    quantity: str = "1",
    purchase_cost: str = "",
    delivery_cost: str = "",
    platform_fee: str = "",
    other_cost: str = "",
) -> str:
    kind = normalize_shipment_kind(shipment_kind)
    lines = [
        "Нове відправлення 📦" if not offline else "Офлайн-продаж 🤍",
        "",
        f"👤 Клієнт: {client_name or 'Unknown'}",
        f"🆔 UID: {user_id}",
        f"{_header_icon(kind)} Тип: {kind}",
    ]
    if offline:
        lines.append("Режим: Офлайн")
        lines.append("⏳ Статус: чекає завершення продажу")
    else:
        lines.append("⏳ Статус: чекає фактичної відправки")
    desc = sanitize_description(description)
    if desc:
        lines.extend(["", f"📝 Опис: {desc}"])
    if cost_text:
        lines.append(f"💰 Ціна продажу: {cost_text}")
    if quantity and quantity != "1":
        lines.append(f"🔢 Кількість: {quantity}")
    if purchase_cost:
        lines.append(f"🏷 Собівартість: {purchase_cost}")
    if delivery_cost:
        lines.append(f"🚚 Доставка: {delivery_cost}")
    if platform_fee:
        lines.append(f"🧾 Комісія: {platform_fee}")
    if other_cost:
        lines.append(f"➖ Інші витрати: {other_cost}")
    if olx_order_id:
        lines.append(f"🛒 OLX ID: {olx_order_id}")
    numbers = ttns or ([ttn] if ttn else [])
    for number in numbers:
        lines.append(f"🔖 ТТН: {number}")
    return "\n".join(lines).strip()[:4000]


def build_ukrposhta_text(
    shipment_kind: str,
    owner_name: str | None,
    description: str | None,
    cost_text: str,
    ttn: str,
    olx_order_id: str = "",
) -> str:
    kind = normalize_shipment_kind(shipment_kind)
    owner = (owner_name or "").strip()
    lines = [f"📮 «УКРПОШТА»{f' {owner}' if owner else ''}", ""]
    desc = sanitize_description(description)
    if desc:
        lines.extend(["📝 Опис:", desc, ""])
    if cost_text:
        lines.append(f"💰 Сума: {cost_text}")
    if olx_order_id:
        lines.append(f"🛒 OLX ID: {olx_order_id}")
    if ttn:
        lines.append(f"🔖 ТТН: {ttn}")
    lines.append(f"Тип: {kind}")
    return "\n".join(lines).strip()[:4000]

