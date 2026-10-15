"""Settlement runner.

Graph: assess -> END.
The assess node loads one claim and applies fixed rules. It does not call a model.
Deductible is subtracted first. Copay is the customer's share of what remains.
The payable amount is then capped at the remaining balance.
"""

from __future__ import annotations

import asyncio

from app.ai_agents.contracts.errors import AgentError
from app.ai_agents.contracts.messages import SettlementNote, SettlementRequest, SettlementResult
from app.ai_agents.settlement.graph import get_settlement_graph
from app.ai_agents.settlement.port import SettlementDataPort
from app.ai_agents.settlement.state import SettlementState


def _error_result(code: str, message: str, retryable: bool) -> SettlementResult:
    return SettlementResult(
        status="error",
        decision="review",
        summary="",
        error=AgentError(code=code, message=message, retryable=retryable),  # type: ignore[arg-type]
    )


async def run_settlement(
    request: SettlementRequest,
    *,
    deps: SettlementDataPort,
    timeout_seconds: float = 30,
) -> SettlementResult:
    graph = get_settlement_graph()
    initial: SettlementState = {"claim_id": request.claim_id}
    try:
        final = await asyncio.wait_for(
            graph.ainvoke(initial, config={"configurable": {"deps": deps}}),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        return _error_result("TIMEOUT", "The settlement review timed out.", True)

    error_code = final.get("error_code")
    if error_code:
        return _error_result(
            error_code,
            final.get("error_message") or "The settlement review failed.",
            error_code == "TIMEOUT",
        )

    decision = final.get("decision") or "review"
    if decision not in {"approve", "partial", "deny", "review"}:
        decision = "review"
    notes = [SettlementNote(**note) for note in final.get("notes") or []]
    return SettlementResult(
        status="ok",
        decision=decision,  # type: ignore[arg-type]
        payable_amount=final.get("payable_amount"),
        notes=notes,
        summary=final.get("summary") or "",
    )
