"""Validation for bookkeeping messages (not parcel creation)."""

from __future__ import annotations

from parser import safe_eval_cost_expr


def validate(data: dict) -> list[str]:
    """Return user-facing errors while preserving the old function name."""

    errors: list[str] = []
    ttns = data.get("ttns") or []
    offline = bool(data.get("offline"))
    cost_text = str(data.get("cost_text") or "").strip()
    description = str(data.get("description") or "").strip()

    if not ttns and not offline:
        errors.append("❌ Додай ТТН або напиши «Офлайн».")
    if not description:
        errors.append("❌ Вкажи товар. Приклад: Товар: Штани Suvene")
    if not cost_text:
        errors.append("❌ Вкажи ціну продажу. Приклад: Ціна: 1200")
    if cost_text and safe_eval_cost_expr(cost_text) is None:
        errors.append("❌ Не вдалося прочитати суму. Приклад: Пошта: 990+100")
    if len(description) > 1000:
        errors.append("❌ Опис задовгий (максимум 1000 символів).")
    if len(ttns) > 1:
        errors.append(
            "❌ Для точного обліку продажів надсилай одну ТТН в одному повідомленні."
        )
    return errors

