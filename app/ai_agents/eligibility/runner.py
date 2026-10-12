"""Eligibility agent.

Graph: retrieve → draft → judge → one critique when groundedness is under 4 → finalize.
Attempt 2 skips the internal critique. The payable amount cannot exceed the ledger.
"""

from __future__ import annotations

import logging
from typing import Any

from app.ai_agents.contracts.errors import AgentError
from app.ai_agents.contracts.messages import (
    CitedClause,
    DisputeAnswer,
    EligibilityAgentRequest,
    EligibilityAgentResult,
    ToolCallRecord,
)
from app.ai_agents.eligibility.graph import get_eligibility_graph
from app.ai_agents.eligibility.state import EligibilityState

logger = logging.getLogger(__name__)


def _error(attempt: int, code: str, message: str) -> EligibilityAgentResult:
    return EligibilityAgentResult(
        status="error",
        attempt=attempt if attempt in (1, 2) else 1,  # type: ignore[arg-type]
        decision="insufficient_evidence",
        payable_amount=0,
        groundedness_score=1,
        needs_manual_review=True,
        manual_review_reasons=[code],
        error=AgentError(code=code, message=message, retryable=False),  # type: ignore[arg-type]
    )


async def run_eligibility_agent(
    request: EligibilityAgentRequest,
    *,
    clauses: Any | None = None,
    llm: Any | None = None,
) -> EligibilityAgentResult:
    if getattr(request, "attempt", None) not in (1, 2):
        bad = _error(1, "BAD_INPUT", "Attempt must be 1 or 2.")
        return EligibilityAgentResult.model_construct(**{**bad.model_dump(), "attempt": getattr(request, "attempt", 1)})
    if request.attempt == 2 and request.revision is None:
        return _error(2, "BAD_INPUT", "A revision is required on attempt 2.")
    logger.info("eligibility agent start claim_id=%s attempt=%s", request.claim_id, request.attempt)
    initial: EligibilityState = {"request": request.model_dump(mode="json"), "critiqued": False, "tool_calls": []}
    final = await get_eligibility_graph().ainvoke(
        initial,
        config={"configurable": {"clauses": clauses, "llm": llm}},
    )
    result = final.get("result") or {}
    return EligibilityAgentResult(
        status="ok",
        attempt=request.attempt,
        decision=result.get("decision") or "insufficient_evidence",
        payable_amount=float(result.get("payable_amount") or 0),
        groundedness_score=int(result.get("groundedness_score") or 1),
        judge_rationale=str(result.get("judge_rationale") or ""),
        citations=[CitedClause.model_validate(item) for item in result.get("citations") or []],
        unsupported_statements=list(result.get("unsupported_statements") or []),
        answered_disputes=[DisputeAnswer.model_validate(item) for item in result.get("answered_disputes") or []],
        needs_manual_review=bool(result.get("needs_manual_review")),
        manual_review_reasons=list(result.get("manual_review_reasons") or []),
        numeric_inconsistency=bool(result.get("numeric_inconsistency")),
        tool_calls=[ToolCallRecord.model_validate(item) for item in result.get("tool_calls") or []],
    )
