"""Review graph. Workers are called through their public functions only."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.ai_agents.contracts.errors import AgentError
from app.ai_agents.contracts.messages import (
    Dispute,
    DocumentAgentRequest,
    DocumentAgentResult,
    DocumentRevision,
    EligibilityAgentRequest,
    EligibilityAgentResult,
    EligibilityRevision,
    FraudAgentRequest,
    FraudAgentResult,
    FraudDocument,
    FraudRevision,
)
from app.ai_agents.orchestrator.policy import (
    decision_reason,
    resolve_proposal,
    revision_kind,
    top_flag,
    watched_changed,
    watched_snapshot,
)
from app.ai_agents.orchestrator.state import OrchestratorState
from app.ai_agents.orchestrator.trace_store import TraceStore, new_event

logger = logging.getLogger(__name__)

DOCUMENT_TIMEOUT = 60
ELIGIBILITY_TIMEOUT = 90
FRAUD_TIMEOUT = 120
_MAX_STEPS = 8

_graph = None


def _configurable(config: RunnableConfig | None) -> dict[str, Any]:
    if not config:
        return {}
    return config.get("configurable") or {}


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")


def flatten_fields(documents: list[dict[str, Any]]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for document in documents:
        fields = document.get("fields") or {}
        if isinstance(fields, dict):
            flat.update(fields)
    return flat


def _source_by_id(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("document_id")): item for item in context.get("documents") or []}


def document_error(attempt: int, code: str, message: str) -> dict[str, Any]:
    return _dump(
        DocumentAgentResult(
            status="error",
            attempt=attempt if attempt in {1, 2} else 1,  # type: ignore[arg-type]
            package_status="needs_reupload",
            kyc_match=False,
            kyc_reason="",
            needs_manual_review=True,
            error=AgentError(code=code, message=message, retryable=code == "TIMEOUT"),  # type: ignore[arg-type]
        )
    )


def eligibility_error(attempt: int, code: str, message: str) -> dict[str, Any]:
    return _dump(
        EligibilityAgentResult(
            status="error",
            attempt=attempt if attempt in {1, 2} else 1,  # type: ignore[arg-type]
            decision="insufficient_evidence",
            payable_amount=0,
            groundedness_score=1,
            needs_manual_review=True,
            manual_review_reasons=[code],
            error=AgentError(code=code, message=message, retryable=code == "TIMEOUT"),  # type: ignore[arg-type]
        )
    )


def fraud_error(attempt: int, code: str, message: str) -> dict[str, Any]:
    return _dump(
        FraudAgentResult(
            status="error",
            attempt=attempt if attempt in {1, 2} else 1,  # type: ignore[arg-type]
            score=0,
            risk_level="MINIMAL",
            critical=False,
            needs_manual_review=True,
            manual_review_reasons=[code],
            error=AgentError(code=code, message=message, retryable=code == "TIMEOUT"),  # type: ignore[arg-type]
        )
    )


async def _call_worker(fn: Any, request: Any, timeout: float) -> Any:
    try:
        return await asyncio.wait_for(fn(request), timeout=timeout)
    except TimeoutError:
        return TimeoutError()
    except Exception:
        logger.exception("worker call failed")
        return RuntimeError("worker failed")


async def _trace(
    store: TraceStore,
    *,
    review_run_id: str,
    claim_id: str,
    from_agent: str,
    to_agent: str,
    kind: str,
    payload: dict[str, Any],
) -> None:
    await store.append(
        new_event(
            review_run_id=review_run_id,
            claim_id=claim_id,
            from_agent=from_agent,
            to_agent=to_agent,
            kind=kind,
            payload=payload,
        )
    )


def build_document_request(state: OrchestratorState, attempt: int) -> DocumentAgentRequest:
    context = state.get("context") or {}
    revision = None
    if attempt == 2:
        missing = (state.get("document") or {}).get("missing_fields") or []
        revision = DocumentRevision(reason="missing fields", focus_fields=[str(item) for item in missing])
    return DocumentAgentRequest(
        claim_id=str(context.get("claim_id") or state.get("claim_id") or ""),
        claim_type=context.get("claim_type") or "HEALTH",
        attempt=attempt,  # type: ignore[arg-type]
        policyholder_name=str(context.get("policyholder_name") or ""),
        policyholder_id_last4=context.get("policyholder_id_last4"),
        documents=context.get("documents") or [],
        revision=revision,
    )


def build_eligibility_request(state: OrchestratorState, attempt: int) -> EligibilityAgentRequest:
    context = state.get("context") or {}
    extracted = flatten_fields((state.get("document") or {}).get("documents") or [])
    revision = None
    if attempt == 2:
        kind = state.get("revision_kind") or ""
        fraud = state.get("fraud") or {}
        eligibility = state.get("eligibility") or {}
        if kind == "disputes":
            revision = EligibilityRevision(
                reason="fraud dispute",
                disputes=[Dispute.model_validate(item) for item in fraud.get("disputes") or []],
                unsupported_statements=[],
            )
        elif kind == "unsupported":
            revision = EligibilityRevision(
                reason="low groundedness",
                disputes=[],
                unsupported_statements=[str(item) for item in eligibility.get("unsupported_statements") or []],
            )
        else:
            revision = EligibilityRevision(reason="document fields updated", disputes=[], unsupported_statements=[])
    return EligibilityAgentRequest(
        claim_id=str(context.get("claim_id") or ""),
        policy_id=str(context.get("policy_id") or ""),
        product_document_id=context.get("product_document_id"),
        claim_type=context.get("claim_type") or "HEALTH",
        claim_amount=float(context.get("claim_amount") or 0),
        incident_date=context.get("incident_date"),
        diagnosis=str(context.get("diagnosis") or ""),
        incident_summary=str(context.get("incident_summary") or ""),
        extracted_fields=extracted,
        ledger=context.get("ledger") or {},
        attempt=attempt,  # type: ignore[arg-type]
        revision=revision,
    )


def build_fraud_request(state: OrchestratorState, attempt: int) -> FraudAgentRequest:
    context = state.get("context") or {}
    sources = _source_by_id(context)
    documents = []
    for item in (state.get("document") or {}).get("documents") or []:
        source = sources.get(str(item.get("document_id")), {})
        documents.append(
            FraudDocument(
                document_id=str(item.get("document_id") or ""),
                type_code=str(item.get("type_code") or source.get("type_code") or ""),
                mime=str(source.get("mime") or "application/pdf"),
                storage_uri=str(source.get("storage_uri") or ""),
                fields=item.get("fields") or {},
            )
        )
    revision = None
    if attempt == 2:
        kind = state.get("revision_kind") or ""
        if kind == "fields":
            changed = watched_changed(state.get("watched_before") or {}, flatten_fields((state.get("document") or {}).get("documents") or []))
            field_name = changed[0] if changed else "bill_total"
            revision = FraudRevision(
                reason="document fields changed: " + ", ".join(changed),
                flag=field_name,
                question=f"The extracted {field_name} changed. Re-check that field.",
            )
        else:
            fraud = state.get("fraud") or {}
            revision = FraudRevision(
                reason="high risk",
                flag=top_flag(fraud),
                question="Cite the evidence for this flag in one sentence.",
            )
    return FraudAgentRequest(
        claim_id=str(context.get("claim_id") or ""),
        claim_type=context.get("claim_type") or "HEALTH",
        claim_amount=float(context.get("claim_amount") or 0),
        incident_date=context.get("incident_date"),
        policy=context.get("policy") or {},
        documents=documents,
        behavior=context.get("behavior") or {},
        attempt=attempt,  # type: ignore[arg-type]
        revision=revision,
    )


async def decide_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    deps = _configurable(config)
    store: TraceStore = deps["store"]
    proposer = deps.get("proposer")
    steps = int(state.get("steps") or 0) + 1
    proposal = None
    if proposer is not None and steps <= _MAX_STEPS:
        try:
            proposal = await proposer(dict(state))
        except Exception:
            logger.exception("action proposal failed")
            proposal = None
    if steps > _MAX_STEPS:
        action, reason_override, rejected = "ESCALATE", "REVISION_CAP", True
    else:
        action, reason_override, rejected = resolve_proposal(dict(state), proposal)
    if rejected:
        await _trace(
            store,
            review_run_id=state["review_run_id"],
            claim_id=state["claim_id"],
            from_agent="system",
            to_agent="orchestrator",
            kind="plan_rejected",
            payload={"proposed": proposal, "used": action, "reason": reason_override or "ILLEGAL_ACTION"},
        )
    reason = reason_override or decision_reason(dict(state))
    await _trace(
        store,
        review_run_id=state["review_run_id"],
        claim_id=state["claim_id"],
        from_agent="system",
        to_agent="orchestrator",
        kind="plan",
        payload={"action": action},
    )
    return {
        "steps": steps,
        "action": action,
        "escalate_reason": reason,
        "revision_kind": revision_kind(dict(state)) if action not in {"ESCALATE", "FINISH"} else state.get("revision_kind", ""),
    }


def route_after_decide(state: OrchestratorState) -> Literal["document", "pair", "eligibility", "fraud", "close"]:
    action = state.get("action")
    if action == "CALL_DOCUMENT" or action == "REVISE_DOCUMENT":
        return "document"
    if action == "CALL_ELIGIBILITY_AND_FRAUD":
        return "pair"
    if action == "REVISE_ELIGIBILITY":
        return "eligibility"
    if action == "REVISE_FRAUD":
        return "fraud"
    return "close"


async def document_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    deps = _configurable(config)
    store: TraceStore = deps["store"]
    worker = deps["workers"]["document"]
    timeout = float(deps.get("document_timeout") or DOCUMENT_TIMEOUT)
    attempt = int(state.get("document_calls") or 0) + 1
    if attempt > 2:
        attempt = 2
    before = flatten_fields((state.get("document") or {}).get("documents") or [])
    request = build_document_request(state, attempt)
    await _trace(
        store,
        review_run_id=state["review_run_id"],
        claim_id=state["claim_id"],
        from_agent="orchestrator",
        to_agent="document",
        kind="handoff",
        payload=_dump(request),
    )
    outcome = await _call_worker(worker, request, timeout)
    if isinstance(outcome, TimeoutError):
        result = document_error(attempt, "TIMEOUT", "The document agent timed out.")
        kind = "error"
    elif isinstance(outcome, Exception):
        result = document_error(attempt, "TOOL_FAILED", "The document agent failed.")
        kind = "error"
    else:
        result = _dump(outcome)
        kind = "result"
    await _trace(
        store,
        review_run_id=state["review_run_id"],
        claim_id=state["claim_id"],
        from_agent="document",
        to_agent="orchestrator",
        kind=kind,
        payload=result,
    )
    update: dict[str, Any] = {"document": result, "document_calls": attempt}
    if attempt == 2:
        after = flatten_fields(result.get("documents") or [])
        update["needs_followup"] = True
        update["watched_before"] = watched_snapshot(before)
        update["watched_fields_changed"] = bool(watched_changed(watched_snapshot(before), watched_snapshot(after)))
    return update


async def _one_worker(
    *,
    store: TraceStore,
    state: OrchestratorState,
    name: str,
    request: Any,
    worker: Any,
    timeout: float,
    outbound_kind: str,
    error_builder: Any,
    attempt: int,
) -> dict[str, Any]:
    await _trace(
        store,
        review_run_id=state["review_run_id"],
        claim_id=state["claim_id"],
        from_agent="orchestrator",
        to_agent=name,
        kind=outbound_kind,
        payload=_dump(request),
    )
    outcome = await _call_worker(worker, request, timeout)
    if isinstance(outcome, TimeoutError):
        result = error_builder(attempt, "TIMEOUT", f"The {name} agent timed out.")
        inbound = "error"
    elif isinstance(outcome, Exception):
        result = error_builder(attempt, "TOOL_FAILED", f"The {name} agent failed.")
        inbound = "error"
    else:
        result = _dump(outcome)
        inbound = "revision" if outbound_kind == "critique" else "result"
    await _trace(
        store,
        review_run_id=state["review_run_id"],
        claim_id=state["claim_id"],
        from_agent=name,
        to_agent="orchestrator",
        kind=inbound,
        payload=result,
    )
    return result


async def _run_logged(
    *,
    store: TraceStore,
    state: OrchestratorState,
    name: str,
    request: Any,
    worker: Any,
    timeout: float,
    attempt: int,
    error_builder: Any,
    outbound_kind: str,
) -> dict[str, Any]:
    outcome = await _call_worker(worker, request, timeout)
    if isinstance(outcome, TimeoutError):
        result = error_builder(attempt, "TIMEOUT", f"The {name} agent timed out.")
        inbound = "error"
    elif isinstance(outcome, Exception):
        result = error_builder(attempt, "TOOL_FAILED", f"The {name} agent failed.")
        inbound = "error"
    else:
        result = _dump(outcome)
        inbound = "revision" if outbound_kind == "critique" else "result"
    await _trace(
        store,
        review_run_id=state["review_run_id"],
        claim_id=state["claim_id"],
        from_agent=name,
        to_agent="orchestrator",
        kind=inbound,
        payload=result,
    )
    return result


async def pair_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    deps = _configurable(config)
    store: TraceStore = deps["store"]
    workers = deps["workers"]
    eligibility_attempt = min(int(state.get("eligibility_calls") or 0) + 1, 2)
    fraud_attempt = min(int(state.get("fraud_calls") or 0) + 1, 2)
    eligibility_request = build_eligibility_request(state, eligibility_attempt)
    fraud_request = build_fraud_request(state, fraud_attempt)
    eligibility_kind = "critique" if eligibility_attempt > 1 else "handoff"
    fraud_kind = "critique" if fraud_attempt > 1 else "handoff"
    await _trace(
        store,
        review_run_id=state["review_run_id"],
        claim_id=state["claim_id"],
        from_agent="orchestrator",
        to_agent="eligibility",
        kind=eligibility_kind,
        payload=_dump(eligibility_request),
    )
    await _trace(
        store,
        review_run_id=state["review_run_id"],
        claim_id=state["claim_id"],
        from_agent="orchestrator",
        to_agent="fraud",
        kind=fraud_kind,
        payload=_dump(fraud_request),
    )

    async def run_eligibility() -> dict[str, Any]:
        return await _run_logged(
            store=store,
            state=state,
            name="eligibility",
            request=eligibility_request,
            worker=workers["eligibility"],
            timeout=float(deps.get("eligibility_timeout") or ELIGIBILITY_TIMEOUT),
            attempt=eligibility_attempt,
            error_builder=eligibility_error,
            outbound_kind=eligibility_kind,
        )

    async def run_fraud() -> dict[str, Any]:
        return await _run_logged(
            store=store,
            state=state,
            name="fraud",
            request=fraud_request,
            worker=workers["fraud"],
            timeout=float(deps.get("fraud_timeout") or FRAUD_TIMEOUT),
            attempt=fraud_attempt,
            error_builder=fraud_error,
            outbound_kind=fraud_kind,
        )

    eligibility_result, fraud_result = await asyncio.gather(run_eligibility(), run_fraud())
    return {
        "eligibility": eligibility_result,
        "fraud": fraud_result,
        "eligibility_calls": int(state.get("eligibility_calls") or 0) + 1,
        "fraud_calls": int(state.get("fraud_calls") or 0) + 1,
        "needs_followup": False,
    }


async def eligibility_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    deps = _configurable(config)
    attempt = min(int(state.get("eligibility_calls") or 0) + 1, 2)
    result = await _one_worker(
        store=deps["store"],
        state=state,
        name="eligibility",
        request=build_eligibility_request(state, attempt),
        worker=deps["workers"]["eligibility"],
        timeout=float(deps.get("eligibility_timeout") or ELIGIBILITY_TIMEOUT),
        outbound_kind="critique" if attempt == 2 else "handoff",
        error_builder=eligibility_error,
        attempt=attempt,
    )
    return {
        "eligibility": result,
        "eligibility_calls": int(state.get("eligibility_calls") or 0) + 1,
        "needs_followup": False,
    }


async def fraud_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    deps = _configurable(config)
    attempt = min(int(state.get("fraud_calls") or 0) + 1, 2)
    result = await _one_worker(
        store=deps["store"],
        state=state,
        name="fraud",
        request=build_fraud_request(state, attempt),
        worker=deps["workers"]["fraud"],
        timeout=float(deps.get("fraud_timeout") or FRAUD_TIMEOUT),
        outbound_kind="critique" if attempt == 2 else "handoff",
        error_builder=fraud_error,
        attempt=attempt,
    )
    return {"fraud": result, "fraud_calls": int(state.get("fraud_calls") or 0) + 1}


async def close_node(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    from app.ai_agents.orchestrator.policy import recommended_action
    from app.ai_agents.orchestrator.summary import write_narrative

    deps = _configurable(config)
    store: TraceStore = deps["store"]
    escalated = state.get("action") == "ESCALATE"
    action = recommended_action(dict(state), escalated=escalated)
    reasons: list[str] = []
    reason = str(state.get("escalate_reason") or "")
    if reason:
        reasons.append(reason)
    elif action == "reject_candidate":
        reasons.append("INELIGIBLE")
    snapshot = {
        "recommended_action": action,
        "reasons": reasons,
        "eligibility": state.get("eligibility"),
        "fraud": state.get("fraud"),
        "document": state.get("document"),
    }
    narrative = await write_narrative(snapshot, llm=deps.get("llm"))
    if escalated:
        await _trace(
            store,
            review_run_id=state["review_run_id"],
            claim_id=state["claim_id"],
            from_agent="orchestrator",
            to_agent="human",
            kind="escalate",
            payload={"reason": reason or "MANUAL_REVIEW", "recommended_action": action},
        )
    return {"recommended_action": action, "reasons": reasons, "narrative": narrative}


def build_orchestrator_graph():
    graph = StateGraph(OrchestratorState)
    graph.add_node("decide", decide_node)
    graph.add_node("document", document_node)
    graph.add_node("pair", pair_node)
    graph.add_node("eligibility", eligibility_node)
    graph.add_node("fraud", fraud_node)
    graph.add_node("close", close_node)
    graph.add_edge(START, "decide")
    graph.add_conditional_edges(
        "decide",
        route_after_decide,
        {
            "document": "document",
            "pair": "pair",
            "eligibility": "eligibility",
            "fraud": "fraud",
            "close": "close",
        },
    )
    graph.add_edge("document", "decide")
    graph.add_edge("pair", "decide")
    graph.add_edge("eligibility", "decide")
    graph.add_edge("fraud", "decide")
    graph.add_edge("close", END)
    return graph.compile()


def get_orchestrator_graph():
    global _graph
    if _graph is None:
        _graph = build_orchestrator_graph()
    return _graph
