"""Fraud forensics agent.

The stored-claim scorer stays in fraud/runner.py. This agent reads the document
fields the orchestrator passes in and returns a FraudAgentResult.
Graph: plan → act → observe, at most six tools, then synthesize.
"""

from __future__ import annotations

import logging
from typing import Any

from app.ai_agents.contracts.errors import AgentError
from app.ai_agents.contracts.messages import Dispute, FraudAgentRequest, FraudAgentResult, ToolCallRecord
from app.ai_agents.fraud.forensics.graph import get_forensics_graph
from app.ai_agents.fraud.forensics.planner import planned_tools, tool_for_flag
from app.ai_agents.fraud.forensics.state import ForensicsState

logger = logging.getLogger(__name__)


def _error(message: str, *, attempt: int = 1) -> FraudAgentResult:
    if attempt not in (1, 2):
        return FraudAgentResult.model_construct(
            status="error",
            attempt=attempt,
            score=0,
            risk_level="MINIMAL",
            critical=False,
            needs_manual_review=True,
            manual_review_reasons=["BAD_INPUT"],
            error=AgentError(code="BAD_INPUT", message=message, retryable=False),
        )
    return FraudAgentResult(
        status="error",
        attempt=attempt,  # type: ignore[arg-type]
        score=0,
        risk_level="MINIMAL",
        critical=False,
        needs_manual_review=True,
        manual_review_reasons=["BAD_INPUT"],
        error=AgentError(code="BAD_INPUT", message=message, retryable=False),
    )


async def run_fraud_agent(request: FraudAgentRequest, *, reader: Any | None = None) -> FraudAgentResult:
    attempt = getattr(request, "attempt", None)
    if attempt not in (1, 2):
        return _error("Attempt must be 1 or 2.", attempt=attempt or 1)
    if attempt == 2 and request.revision is None:
        return _error("A revision is required on attempt 2.", attempt=2)
    payload = request.model_dump(mode="json")
    if attempt == 2:
        queue = [tool_for_flag(request.revision.flag)]  # type: ignore[union-attr]
    else:
        queue = planned_tools(payload)
    logger.info("fraud agent start claim_id=%s tools=%s", request.claim_id, queue)
    initial: ForensicsState = {"request": payload, "queue": queue}
    final = await get_forensics_graph().ainvoke(initial, config={"configurable": {"reader": reader}})
    result = final.get("result") or {}
    if result.get("status") == "error":
        return FraudAgentResult(
            status="error",
            attempt=attempt,  # type: ignore[arg-type]
            score=0,
            risk_level="MINIMAL",
            critical=False,
            tools_used=list(result.get("tools_used") or []),
            needs_manual_review=True,
            explanation=str(result.get("error_message") or "The fraud tools failed."),
            error=AgentError(
                code="TOOL_FAILED",
                message=str(result.get("error_message") or "The fraud tools failed."),
                retryable=False,
            ),
        )
    return FraudAgentResult(
        status="ok",
        attempt=attempt,  # type: ignore[arg-type]
        score=float(result.get("score") or 0),
        risk_level=result.get("risk_level") or "MINIMAL",
        flags=list(result.get("flags") or []),
        critical=bool(result.get("critical")),
        tools_used=list(result.get("tools_used") or []),
        tool_calls=[
            ToolCallRecord(
                tool_name=str(item.get("tool_name") or ""),
                ok=bool(item.get("ok")),
                summary=str(item.get("evidence") or ""),
            )
            for item in result.get("observations") or []
        ],
        explanation=str(result.get("explanation") or ""),
        disputes=[Dispute.model_validate(item) for item in result.get("disputes") or []],
        needs_manual_review=bool(result.get("needs_manual_review")),
        manual_review_reasons=list(result.get("manual_review_reasons") or []),
    )
