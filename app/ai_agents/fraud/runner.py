"""Fraud review runner.

Graph: review -> END.
The review node loads one claim and applies fixed rules. It does not call a model.
"""

from __future__ import annotations

import asyncio

from app.ai_agents.contracts.errors import AgentError
from app.ai_agents.contracts.messages import FraudFlag, FraudReviewRequest, FraudReviewResult
from app.ai_agents.fraud.graph import get_fraud_graph
from app.ai_agents.fraud.port import FraudDataPort
from app.ai_agents.fraud.state import FraudState


def _error_result(code: str, message: str, retryable: bool) -> FraudReviewResult:
    return FraudReviewResult(
        status="error",
        risk="low",
        summary="",
        error=AgentError(code=code, message=message, retryable=retryable),  # type: ignore[arg-type]
    )


async def run_fraud_review(
    request: FraudReviewRequest,
    *,
    deps: FraudDataPort,
    timeout_seconds: float = 30,
) -> FraudReviewResult:
    graph = get_fraud_graph()
    initial: FraudState = {"claim_id": request.claim_id}
    try:
        final = await asyncio.wait_for(
            graph.ainvoke(initial, config={"configurable": {"deps": deps}}),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        return _error_result("TIMEOUT", "The fraud review timed out.", True)

    error_code = final.get("error_code")
    if error_code:
        return _error_result(
            error_code,
            final.get("error_message") or "The fraud review failed.",
            error_code == "TIMEOUT",
        )

    risk = final.get("risk") or "low"
    if risk not in {"low", "medium", "high"}:
        risk = "low"
    flags = [FraudFlag(**flag) for flag in final.get("flags") or []]
    return FraudReviewResult(
        status="ok",
        risk=risk,  # type: ignore[arg-type]
        flags=flags,
        summary=final.get("summary") or "",
    )
