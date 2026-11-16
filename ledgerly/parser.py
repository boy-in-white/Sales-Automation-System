"""Parsing helpers for short Ukrainian OLX/bookkeeping messages."""

from __future__ import annotations

import ast
import operator
import re
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Optional


STOP_WORDS = {
    "тел", "телефон", "phone", "піб", "фио", "fio", "name",
    "місто", "город", "city", "м", "м.", "відділення", "відд",
    "відд.", "warehouse", "номер", "ном.", "№", "#", "сума",
    "вартість", "ціна", "cost", "price", "пошта", "опис",
    "description", "товар", "коментар", "note", "одержувач",
    "recipient", "отримувач", "клієнт", "client", "тип", "type",
    "приватне замовлення", "співробітники", "офлайн", "offline",
    "олх", "olx", "фоп", "наложка", "ттн", "tracking",
}

HEADING_WORDS = {
    "фоп", "наложка", "олх", "olx", "офлайн", "offline",
}

NOISE_LINE_RE = re.compile(r"^[✅❌☑️✔️⚠️ℹ️\s]+$")
TTN_RE = re.compile(r"(?<!\d)(?:\d[\d\s\-]{11,25}\d)(?!\d)")
MAX_COST_EXPRESSION_LENGTH = 80
MAX_COST_VALUE = Decimal("10000000")


def clean_line(line: str) -> str:
    line = (line or "").strip()
    line = re.sub(r"^[^\wА-ЯІЇЄа-яіїє0-9]+", "", line, flags=re.UNICODE)
    line = re.sub(r"[^\wА-ЯІЇЄа-яіїє0-9]+$", "", line, flags=re.UNICODE)
    return re.sub(r"\s+", " ", line).strip(",.;: ")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"[;|]+", "\n", text.replace("\r", "\n"))


def split_lines(text: str) -> list[str]:
    parts = [clean_line(item) for item in normalize_text(text).split("\n")]
    return [item for item in parts if item and not NOISE_LINE_RE.match(item)]


def normalize_ttn_candidate(value: str) -> Optional[str]:
    digits = re.sub(r"\D", "", value or "")
    return digits if len(digits) in (13, 14) else None


def extract_ttns(text: str) -> list[str]:
    found: list[str] = []
    for match in TTN_RE.finditer(text or ""):
        ttn = normalize_ttn_candidate(match.group())
        if ttn:
            found.append(ttn)
    for line in split_lines(text or ""):
        ttn = normalize_ttn_candidate(line)
        if ttn:
            found.append(ttn)
    return list(dict.fromkeys(found))


def detect_shipment_kind(text: str) -> str:
    low = (text or "").lower()
    if "офлайн" in low or "offline" in low:
        return "Офлайн"
    if "фоп" in low:
        return "ФОП"
    if "налож" in low:
        return "Наложка"
    if "олх" in low or "olx" in low:
        return "ОЛХ"
    return "Наложка"


def _bounded(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) > MAX_COST_VALUE:
        raise ValueError("Cost is outside the accepted range")
    return value


def _eval_cost_node(node: ast.AST, budget: list[int]) -> Decimal:
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("Expression is too complex")
    if isinstance(node, ast.Expression):
        return _eval_cost_node(node.body, budget)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return _bounded(Decimal(str(node.value)))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_cost_node(node.operand, budget)
        return _bounded(value if isinstance(node.op, ast.UAdd) else -value)
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
    ):
        left = _eval_cost_node(node.left, budget)
        right = _eval_cost_node(node.right, budget)
        operation = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
        }[type(node.op)]
        return _bounded(operation(left, right))
    raise ValueError("Unsupported cost expression")


def safe_eval_cost_expr(expr: str | None) -> Optional[str]:
    if not expr:
        return None
    normalized = str(expr).strip().replace(" ", "").replace(",", ".")
    if not normalized or len(normalized) > MAX_COST_EXPRESSION_LENGTH:
        return None
    if not re.fullmatch(r"[0-9.+\-*/()]+", normalized):
        return None
    try:
        value = _eval_cost_node(ast.parse(normalized, mode="eval"), [32])
    except (SyntaxError, ValueError, TypeError, InvalidOperation, DivisionByZero, ZeroDivisionError):
        return None
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", ""} else rendered


def _looks_like_phone_or_ttn(value: str) -> bool:
    digits = re.sub(r"\D", "", value or "")
    return len(digits) in (10, 12, 13, 14)


def extract_cost_text(text: str) -> Optional[str]:
    lines = split_lines(text or "")
    for line in lines:
        match = re.search(
            r"\b(?:ціна продажу|ціна|пошта|cost|price|сума|вартість|оплата)\s*:\s*"
            r"([0-9][0-9\s+\-*/().,]*)",
            line,
            re.I,
        )
        if match:
            candidate = clean_line(match.group(1))
            return candidate if safe_eval_cost_expr(candidate) is not None else candidate

    for line in lines:
        match = re.search(r"\b([0-9][0-9\s+\-*/().,]*)\s*(?:грн|uah|₴)\b", line, re.I)
        if match and not _looks_like_phone_or_ttn(match.group(1)):
            return clean_line(match.group(1))

    for line in lines:
        if not re.fullmatch(r"[0-9][0-9\s+\-*/().,]*", line):
            continue
        if _looks_like_phone_or_ttn(line):
            continue
        candidate = clean_line(line)
        if len(re.sub(r"\D", "", candidate)) <= 7 and safe_eval_cost_expr(candidate) is not None:
            return candidate
    return None


def extract_cost(text: str) -> Optional[str]:
    return safe_eval_cost_expr(extract_cost_text(text))


def extract_money_field(text: str, labels: tuple[str, ...]) -> tuple[Optional[str], Optional[str]]:
    label_pattern = "|".join(re.escape(label) for label in labels)
    for line in split_lines(text or ""):
        match = re.search(
            rf"^(?:{label_pattern})\s*:\s*([0-9][0-9\s+\-*/().,]*)",
            line,
            re.I,
        )
        if match:
            raw = clean_line(match.group(1))
            return raw, safe_eval_cost_expr(raw)
    return None, None


def extract_quantity(text: str) -> str:
    match = re.search(r"(?:кількість|к-сть|qty|quantity)\s*:\s*(\d{1,4})\b", text or "", re.I)
    return match.group(1) if match else "1"


def extract_phone(text: str) -> Optional[str]:
    for match in re.finditer(r"(?<!\d)(?:\+?38)?\s*0\d[\d\s\-()]{8,15}(?!\d)", text or ""):
        digits = re.sub(r"\D", "", match.group())
        if len(digits) == 10 and digits.startswith("0"):
            return "38" + digits
        if len(digits) == 12 and digits.startswith("380"):
            return digits
    return None


def extract_warehouse(text: str) -> Optional[str]:
    patterns = (
        r"(?:відділення|відд|warehouse)\s*(?:номер\s*)?(?:№\s*)?(\d{1,4})\b",
        r"(?:№|#)\s*(\d{1,4})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", re.I)
        if match:
            return match.group(1)
    return None


def extract_pib(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    match = re.search(
        r"(?:піб|fio|name)\s*[:\-]?\s+"
        r"([А-ЯІЇЄA-Z][а-яіїєa-z'\-]+)\s+"
        r"([А-ЯІЇЄA-Z][а-яіїєa-z'\-]+)\s+"
        r"([А-ЯІЇЄA-Z][а-яіїєa-z'\-]+)",
        text or "",
        re.I,
    )
    if match:
        return tuple(clean_line(match.group(i)) for i in (1, 2, 3))  # type: ignore[return-value]
    return None, None, None


def extract_city(text: str, lines: Optional[list[str]] = None) -> Optional[str]:
    lines = lines if lines is not None else split_lines(text or "")
    for line in lines:
        match = re.search(
            r"\b(?:м\.?|місто|city|город)\s*[:\-]?\s*"
            r"([A-Za-zА-ЯІЇЄа-яіїє'\- ]{2,50})",
            line,
            re.I,
        )
        if match:
            candidate = re.split(
                r"\b(?:обл\.?|область|район|р-н|відділення|відд\.?|вул\.?|вулиця)\b",
                match.group(1),
                maxsplit=1,
                flags=re.I,
            )[0]
            candidate = clean_line(candidate)
            if candidate and not re.search(r"\d", candidate):
                return candidate.title()
    for line in lines:
        if not re.search(r"\b(?:обл\.?|область|район|р-н)\b", line, re.I):
            continue
        candidate = clean_line(re.split(r"[,;]", line, maxsplit=1)[0])
        if candidate and not re.search(r"\d", candidate) and len(candidate) <= 50:
            return candidate.title()
    return None


def extract_olx_order_id(text: str) -> Optional[str]:
    match = re.search(
        r"\b(?:olx|олх)\s*(?:order|замовлення|id|№|#)?\s*[:#№-]?\s*([A-Z0-9-]{4,40})\b",
        text or "",
        re.I,
    )
    if not match:
        return None
    value = match.group(1).strip()
    return None if value.lower() in {"доставка", "delivery"} else value


def _is_metadata_line(line: str) -> bool:
    low = clean_line(line).lower()
    if not low or low in STOP_WORDS or low in HEADING_WORDS:
        return True
    if normalize_ttn_candidate(line) or extract_phone(line):
        return True
    # A bare price is metadata too, even without a "Ціна:" label.
    amount = re.sub(r"\s*(?:грн\.?|uah|₴)\s*$", "", line, flags=re.I).strip()
    if safe_eval_cost_expr(amount) is not None:
        return True
    prefixes = (
        "піб", "fio", "name", "тел", "phone", "місто", "city", "город",
        "клієнт", "client", "тип", "type", "відділення", "відд", "warehouse",
        "ттн", "tracking", "uid", "приватне замовлення", "співробітники",
    )
    if low.startswith(prefixes):
        return True
    if re.match(
        r"^(?:ціна продажу|пошта|сума|вартість|ціна|cost|price|оплата|собівартість|закупка|"
        r"доставка|delivery|комісія|fee|інші витрати|інше|other|кількість|к-сть|qty)\s*:",
        low,
    ):
        return True
    if re.match(r"^(?:olx|олх)\s*(?:order|замовлення|id|№|#|:)", low):
        return True
    return False


def extract_description(text: str, lines: Optional[list[str]] = None) -> Optional[str]:
    raw = text or ""
    labelled = re.search(r"(?:опис|description|товар)\s*[:\-]?\s*(.+)", raw, re.I)
    if labelled:
        return clean_line(labelled.group(1))[:1000] or None

    lines = lines if lines is not None else split_lines(raw)
    candidates = [line for line in lines if not _is_metadata_line(line)]
    if len(candidates) >= 2:
        first = candidates[0]
        # Never discard an arbitrary short product name as a customer name.
        # Keep unlabelled name headers only when they look like a full name.
        if re.fullmatch(
            r"[А-ЯІЇЄҐ][а-яіїєґ'’ʼ\-]+(?:\s+[А-ЯІЇЄҐ][а-яіїєґ'’ʼ\-]+){1,2}",
            first,
        ):
            candidates = candidates[1:]
    result = "\n".join(candidates).strip()
    return result[:1000] or None


def parse_message(text: str) -> dict[str, str]:
    raw = (text or "").strip()
    lines = split_lines(raw)
    cost_text = extract_cost_text(raw)
    purchase_raw, purchase_cost = extract_money_field(raw, ("собівартість", "закупка", "purchase cost"))
    delivery_raw, delivery_cost = extract_money_field(raw, ("доставка", "delivery"))
    fee_raw, platform_fee = extract_money_field(raw, ("комісія olx", "комісія", "fee"))
    other_raw, other_cost = extract_money_field(raw, ("інші витрати", "інше", "other"))
    description = extract_description(raw, lines)
    data: dict[str, Optional[str]] = {
        "shipment_kind": detect_shipment_kind(raw),
        "phone": extract_phone(raw),
        "cost_text": cost_text,
        "cost": safe_eval_cost_expr(cost_text),
        "sale_price": safe_eval_cost_expr(cost_text),
        "purchase_cost_text": purchase_raw,
        "purchase_cost": purchase_cost,
        "delivery_cost_text": delivery_raw,
        "delivery_cost": delivery_cost,
        "platform_fee_text": fee_raw,
        "platform_fee": platform_fee,
        "other_cost_text": other_raw,
        "other_cost": other_cost,
        "quantity": extract_quantity(raw),
        "warehouse": extract_warehouse(raw),
        "city": extract_city(raw, lines),
        "description": description,
        "product_name": description,
        "olx_order_id": extract_olx_order_id(raw),
    }
    last, first, middle = extract_pib(raw)
    data.update({"last": last, "first": first, "middle": middle})
    return {key: value for key, value in data.items() if value is not None}

