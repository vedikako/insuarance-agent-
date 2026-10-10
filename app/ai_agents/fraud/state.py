from __future__ import annotations

from typing import Any, TypedDict


class FraudState(TypedDict, total=False):
    claim_id: str
    claim: dict[str, Any]
    risk: str
    flags: list[dict[str, Any]]
    summary: str
    error_code: str
    error_message: str
