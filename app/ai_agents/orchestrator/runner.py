"""Review orchestrator.

Graph: decide → one worker step → decide, until FINISH or ESCALATE.
The first action is always the document agent. Eligibility and fraud run together
only after the package is accepted or partial. Each worker is called at most twice.
This function does not set claim status and it does not call policy Q&A.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_agents.contracts.messages import (
    DocumentAgentResult,
    EligibilityAgentResult,
    FraudAgentResult,
    FraudBehavior,
    FraudPolicy,
    LedgerSnapshot,
    ReviewPackage,
    ReviewRequest,
)
from app.ai_agents.orchestrator.graph import get_orchestrator_graph
from app.ai_agents.orchestrator.state import OrchestratorState
from app.ai_agents.orchestrator.summary import write_narrative
from app.ai_agents.orchestrator.trace_store import MemoryTraceStore, SqlTraceStore, TraceStore, new_event

logger = logging.getLogger(__name__)

_PLAN_PROMPT = (
    "Propose the single next review action. "
    "Return JSON {\"action\": \"CALL_DOCUMENT\" | \"CALL_ELIGIBILITY_AND_FRAUD\" | "
    "\"REVISE_DOCUMENT\" | \"REVISE_ELIGIBILITY\" | \"REVISE_FRAUD\" | \"ESCALATE\" | \"FINISH\"}."
)


def _money(value: Any) -> float:
    return round(float(value or 0), 2)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return str(value)


def context_from_rows(
    *,
    claim_id: str,
    policy_id: str,
    claim_amount: Any,
    policy_type: Any,
    start_date: Any,
    end_date: Any,
    sum_insured: Any,
    insured_name: Any,
    utilized: Any,
    documents: list[dict[str, Any]] | None = None,
    recent_claims_30d: int = 0,
    total_claim_amount_90d: float = 0,
    diagnosis: str = "",
    incident_summary: str = "",
    incident_date: str | None = None,
    policyholder_id_last4: str | None = None,
) -> dict[str, Any]:
    """Build the facts the orchestrator copies into worker requests."""
    sum_value = _money(sum_insured)
    used = _money(utilized)
    claim_type = str(policy_type or "HEALTH").upper()
    if claim_type not in {"HEALTH", "MOTOR"}:
        claim_type = "HEALTH"
    return {
        "claim_id": claim_id,
        "policy_id": str(policy_id),
        "claim_type": claim_type,
        "claim_amount": _money(claim_amount),
        "incident_date": incident_date,
        "diagnosis": diagnosis,
        "incident_summary": incident_summary,
        "policyholder_name": str(insured_name or ""),
        "policyholder_id_last4": policyholder_id_last4,
        "product_document_id": None,
        "documents": list(documents or []),
        "policy": FraudPolicy(
            start_date=_iso(start_date) or "",
            end_date=_iso(end_date) or "",
            sum_insured=sum_value,
            holder_name=str(insured_name or ""),
        ).model_dump(mode="json"),
        "ledger": LedgerSnapshot(
            sum_insured=sum_value,
            remaining_balance=round(max(sum_value - used, 0.0), 2),
            amount_already_paid=used,
        ).model_dump(mode="json"),
        "behavior": FraudBehavior(
            recent_claims_30d=recent_claims_30d,
            total_claim_amount_90d=_money(total_claim_amount_90d),
            prior_fraud_flags=0,
        ).model_dump(mode="json"),
    }


async def load_claim_context(db: AsyncSession, claim_id: str) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            """
            SELECT c.claim_amount, c.policy_id, p.policy_type, p.start_date, p.end_date,
                   p.sum_insured, p.insured_name
            FROM claims c
            JOIN policies p ON p.id = c.policy_id
            WHERE c.id = :claim_id
            """
        ),
        {"claim_id": claim_id},
    )
    row = result.first()
    if row is None:
        return None
    balance = await db.execute(
        text(
            """
            SELECT COALESCE(SUM(amount_delta), 0) AS utilized
            FROM policy_utilization_entries
            WHERE policy_id = :policy_id AND coverage_bucket = 'BASE'
            """
        ),
        {"policy_id": str(row.policy_id)},
    )
    recent = await db.execute(
        text(
            """
            SELECT COUNT(*) FILTER (
                       WHERE updated_at > NOW() - INTERVAL '30 days'
                   ) AS claims_30d,
                   COALESCE(SUM(claim_amount) FILTER (
                       WHERE updated_at > NOW() - INTERVAL '90 days'
                   ), 0) AS amount_90d
            FROM claims
            WHERE policy_id = :policy_id
              AND id <> :claim_id
            """
        ),
        {"policy_id": str(row.policy_id), "claim_id": claim_id},
    )
    counts = recent.first()
    return context_from_rows(
        claim_id=claim_id,
        policy_id=str(row.policy_id),
        claim_amount=row.claim_amount,
        policy_type=row.policy_type,
        start_date=row.start_date,
        end_date=row.end_date,
        sum_insured=row.sum_insured,
        insured_name=row.insured_name,
        utilized=balance.scalar(),
        recent_claims_30d=int(counts.claims_30d or 0) if counts else 0,
        total_claim_amount_90d=float(counts.amount_90d or 0) if counts else 0,
    )


def _model_allowed(use_model: bool | None) -> bool:
    if use_model is False:
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and use_model is not True:
        return False
    from app.config import settings

    return bool(settings.GCP_API_KEY)


def _llm():
    from app.config import settings
    from app.ai_agents.policy_qa.llm import GeminiJsonLLM

    return GeminiJsonLLM(settings.GCP_API_KEY)


async def _propose(state: dict[str, Any]) -> str | None:
    from app.ai_agents.orchestrator.summary import _parse_json_object

    raw = await _llm().complete(_PLAN_PROMPT, str({
        "document_calls": state.get("document_calls", 0),
        "eligibility_calls": state.get("eligibility_calls", 0),
        "fraud_calls": state.get("fraud_calls", 0),
        "package_status": (state.get("document") or {}).get("package_status"),
        "decision": (state.get("eligibility") or {}).get("decision"),
        "groundedness_score": (state.get("eligibility") or {}).get("groundedness_score"),
        "fraud_critical": (state.get("fraud") or {}).get("critical"),
        "risk_level": (state.get("fraud") or {}).get("risk_level"),
    }))
    action = str(_parse_json_object(raw).get("action") or "")
    return action or None


def _failed_package(claim_id: str, review_run_id: str, code: str, message: str) -> ReviewPackage:
    return ReviewPackage(
        review_run_id=review_run_id,
        claim_id=claim_id,
        run_status="failed",
        recommended_action="manual_review",
        reasons=[code],
        narrative=message,
    )


async def _default_workers() -> dict[str, Any]:
    from app.ai_agents.document.runner import run_document_agent
    from app.ai_agents.eligibility.runner import run_eligibility_agent
    from app.ai_agents.fraud.runner import run_fraud_agent

    return {
        "document": run_document_agent,
        "eligibility": run_eligibility_agent,
        "fraud": run_fraud_agent,
    }


def _validate(model: type[Any], payload: dict[str, Any] | None) -> Any:
    if not payload:
        return None
    return model.model_validate(payload)


async def run_review(
    request: ReviewRequest,
    db: AsyncSession | None = None,
    *,
    context: dict[str, Any] | None = None,
    workers: dict[str, Any] | None = None,
    store: TraceStore | None = None,
    proposer: Any | None = None,
    use_model: bool | None = None,
    document_timeout: float | None = None,
    eligibility_timeout: float | None = None,
    fraud_timeout: float | None = None,
) -> ReviewPackage:
    review_store: TraceStore = store or (SqlTraceStore(db) if db is not None else MemoryTraceStore())
    existing = await review_store.find_running(request.claim_id)
    if existing:
        return await review_store.running_package(request.claim_id, existing)

    review_run_id = str(uuid4())
    if context is None and db is not None:
        context = await load_claim_context(db, request.claim_id)
    if context is None:
        package = _failed_package(request.claim_id, review_run_id, "BAD_INPUT", "Claim not found.")
        return package
    if not context.get("documents"):
        started = await review_store.start(request.claim_id, review_run_id)
        if started != review_run_id:
            return await review_store.running_package(request.claim_id, started)
        await review_store.append(
            new_event(
                review_run_id=review_run_id,
                claim_id=request.claim_id,
                from_agent="system",
                to_agent="orchestrator",
                kind="plan",
                payload={"action": "CALL_DOCUMENT"},
            )
        )
        await review_store.append(
            new_event(
                review_run_id=review_run_id,
                claim_id=request.claim_id,
                from_agent="orchestrator",
                to_agent="human",
                kind="escalate",
                payload={"reason": "NEEDS_REUPLOAD"},
            )
        )
        package = ReviewPackage(
            review_run_id=review_run_id,
            claim_id=request.claim_id,
            run_status="escalated",
            recommended_action="manual_review",
            reasons=["NEEDS_REUPLOAD"],
            narrative="The claim has no documents to review.",
            trace=await review_store.events(review_run_id),
        )
        await review_store.finish(request.claim_id, review_run_id, package)
        return package

    if workers is None:
        try:
            workers = await _default_workers()
        except Exception:
            logger.exception("review workers are not available")
            package = _failed_package(
                request.claim_id,
                review_run_id,
                "TOOL_FAILED",
                "The review workers are not available.",
            )
            return package

    started = await review_store.start(request.claim_id, review_run_id)
    if started != review_run_id:
        return await review_store.running_package(request.claim_id, started)

    allow_model = _model_allowed(use_model)
    llm = _llm() if allow_model else None
    initial: OrchestratorState = {
        "review_run_id": review_run_id,
        "claim_id": request.claim_id,
        "context": context,
        "document": None,
        "eligibility": None,
        "fraud": None,
        "document_calls": 0,
        "eligibility_calls": 0,
        "fraud_calls": 0,
        "needs_followup": False,
        "steps": 0,
    }
    try:
        final = await get_orchestrator_graph().ainvoke(
            initial,
            config={
                "configurable": {
                    "store": review_store,
                    "workers": workers,
                    "proposer": proposer if proposer is not None else (_propose if allow_model else None),
                    "llm": llm,
                    "document_timeout": document_timeout,
                    "eligibility_timeout": eligibility_timeout,
                    "fraud_timeout": fraud_timeout,
                }
            },
        )
    except Exception:
        logger.exception("review orchestrator failed")
        package = _failed_package(request.claim_id, review_run_id, "TOOL_FAILED", "The review failed.")
        await review_store.finish(request.claim_id, review_run_id, package)
        return package

    escalated = final.get("action") == "ESCALATE"
    action = str(final.get("recommended_action") or "manual_review")
    if action not in {"approve_candidate", "reject_candidate", "manual_review"}:
        action = "manual_review"
    narrative = str(final.get("narrative") or "")
    if not narrative:
        narrative = await write_narrative(
            {
                "recommended_action": action,
                "reasons": list(final.get("reasons") or []),
                "eligibility": final.get("eligibility"),
            },
            llm=llm,
        )
    package = ReviewPackage(
        review_run_id=review_run_id,
        claim_id=request.claim_id,
        run_status="escalated" if escalated else "ready",
        recommended_action=action,  # type: ignore[arg-type]
        reasons=list(final.get("reasons") or []),
        document=_validate(DocumentAgentResult, final.get("document")),
        eligibility=_validate(EligibilityAgentResult, final.get("eligibility")),
        fraud=_validate(FraudAgentResult, final.get("fraud")),
        narrative=narrative,
        trace=await review_store.events(review_run_id),
    )
    await review_store.finish(request.claim_id, review_run_id, package)
    return package
