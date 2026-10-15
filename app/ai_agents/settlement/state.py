from __future__ import annotations

from typing import Any, TypedDict


class SettlementState(TypedDict, total=False):
    claim_id: str
    claim: dict[str, Any]
    decision: str
    payable_amount: float | None
    notes: list[dict[str, Any]]
    summary: str
    error_code: str
    error_message: str
