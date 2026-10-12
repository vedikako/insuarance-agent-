from __future__ import annotations

from typing import Any, TypedDict


class OrchestratorState(TypedDict, total=False):
    review_run_id: str
    claim_id: str
    context: dict[str, Any]
    document: dict[str, Any] | None
    eligibility: dict[str, Any] | None
    fraud: dict[str, Any] | None
    document_calls: int
    eligibility_calls: int
    fraud_calls: int
    needs_followup: bool
    watched_before: dict[str, Any]
    watched_fields_changed: bool
    revision_kind: str
    action: str
    escalate_reason: str
    recommended_action: str
    reasons: list[str]
    narrative: str
    steps: int
