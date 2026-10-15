from __future__ import annotations

from typing import Any


def money(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def line_item_total(fields: dict[str, Any]) -> float | None:
    items = fields.get("line_items")
    if not isinstance(items, list) or not items:
        return None
    total = 0.0
    seen = False
    for item in items:
        amount = money(item.get("amount") if isinstance(item, dict) else item)
        if amount is None:
            continue
        total += amount
        seen = True
    if not seen:
        return None
    return round(total, 2)


def payable_limit(claim_amount: float, remaining_balance: float, fields: dict[str, Any]) -> float:
    limit = min(float(claim_amount), float(remaining_balance))
    line_total = line_item_total(fields)
    if line_total is not None:
        limit = min(limit, line_total)
    return round(max(limit, 0.0), 2)


def decision_for(payable: float, claim_amount: float) -> str:
    if payable <= 0:
        return "ineligible"
    if payable + 0.01 < float(claim_amount):
        return "partially_eligible"
    return "eligible"
