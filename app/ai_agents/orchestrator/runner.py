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


_DOCUMENT_TYPES = {
    "HOSPITAL_BILL",
    "DISCHARGE_SUMMARY",
    "AADHAAR",
    "PAN",
    "REPAIR_ESTIMATE",
    "FIR",
    "RC",
}
_MIME = {
    "application/pdf": "application/pdf",
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
}


def _cell(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def document_mime(content_type: Any, filename: Any) -> str | None:
    raw = str(content_type or "").split(";")[0].strip().lower()
    if raw in _MIME:
        return _MIME[raw]
    name = str(filename or "").lower()
    if name.endswith(".pdf"):
        return "application/pdf"
    if name.endswith(".png"):
        return "image/png"
    if name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    return None


def id_last4(number: Any) -> str | None:
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    if len(digits) < 4:
        return None
    return digits[-4:]


def document_refs_from_rows(rows: list[Any]) -> list[dict[str, str]]:
    """Keep only files the document agent knows how to read."""
    refs: list[dict[str, str]] = []
    for row in rows:
        type_code = str(_cell(row, "document_type_code") or "").upper()
        if type_code not in _DOCUMENT_TYPES:
            continue
        filename = str(_cell(row, "original_filename") or f"{type_code.lower()}.bin")
        mime = document_mime(_cell(row, "content_type"), filename)
        storage_uri = str(_cell(row, "storage_path") or "").strip()
        document_id = str(_cell(row, "id") or "")
        if mime is None or not storage_uri or not document_id:
            continue
        refs.append(
            {
                "document_id": document_id,
                "type_code": type_code,
                "mime": mime,
                "storage_uri": storage_uri,
                "filename": filename,
            }
        )
    return refs


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
    claim_type: Any | None = None,
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
    raw_type = claim_type if claim_type not in (None, "") else policy_type
    claim_type = str(raw_type or "HEALTH").upper()
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
            SELECT c.claim_amount, c.policy_id, c.user_id, c.claim_type, c.incident_date,
                   c.description, c.situation_details, p.policy_type, p.start_date, p.end_date,
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
    files = await db.execute(
        text(
            """
            SELECT id, document_type_code, storage_path, original_filename, content_type, diagnosis
            FROM claim_documents
            WHERE claim_id = :claim_id
            ORDER BY id
            """
        ),
        {"claim_id": claim_id},
    )
    file_rows = list(files)
    details = row.situation_details if isinstance(row.situation_details, dict) else {}
    diagnosis = str(details.get("diagnosis") or "")
    if not diagnosis:
        for item in file_rows:
            if item.diagnosis:
                diagnosis = str(item.diagnosis)
                break
    identity = await db.execute(
        text(
            """
            SELECT document_number
            FROM kyc_documents
            WHERE user_id = :user_id
              AND is_active = true
              AND document_type IN ('AADHAAR', 'PAN')
              AND document_number IS NOT NULL
            ORDER BY CASE document_type WHEN 'AADHAAR' THEN 0 ELSE 1 END
            LIMIT 1
            """
        ),
        {"user_id": str(row.user_id)},
    )
    identity_row = identity.first()
    return context_from_rows(
        claim_id=claim_id,
        policy_id=str(row.policy_id),
        claim_amount=row.claim_amount,
        claim_type=row.claim_type,
        policy_type=row.policy_type,
        start_date=row.start_date,
        end_date=row.end_date,
        sum_insured=row.sum_insured,
        insured_name=row.insured_name,
        utilized=balance.scalar(),
        documents=document_refs_from_rows(file_rows),
        recent_claims_30d=int(counts.claims_30d or 0) if counts else 0,
        total_claim_amount_90d=float(counts.amount_90d or 0) if counts else 0,
        diagnosis=diagnosis,
        incident_summary=str(row.description or ""),
        incident_date=_iso(row.incident_date),
        policyholder_id_last4=id_last4(identity_row.document_number) if identity_row else None,
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


async def _default_workers(db: AsyncSession | None, llm: Any | None) -> dict[str, Any]:
    """Document and eligibility are Vedika's review workers. Fraud is called beside them."""
    from app.ai_agents.document.runner import run_document_agent
    from app.ai_agents.eligibility.runner import run_eligibility_agent
    from app.ai_agents.fraud.runner import run_fraud_agent
    from app.services.policy_retrieval import rank_chunks, retrieve_clauses
    from app.services.review_inputs import registered_clauses

    async def document(request: Any) -> Any:
        return await run_document_agent(request, llm=llm)

    async def clauses(policy_id: str, query: str) -> list[dict[str, Any]]:
        registered = registered_clauses(policy_id)
        if registered is not None:
            return rank_chunks(registered, query, k=4)
        if db is None:
            return []
        return await retrieve_clauses(policy_id, query, db)

    async def eligibility(request: Any) -> Any:
        return await run_eligibility_agent(request, clauses=clauses, llm=llm)

    async def fraud(request: Any) -> Any:
        return await run_fraud_agent(request)

    return {"document": document, "eligibility": eligibility, "fraud": fraud}


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

    allow_model = _model_allowed(use_model)
    llm = _llm() if allow_model else None
    if workers is None:
        try:
            workers = await _default_workers(db, llm)
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
