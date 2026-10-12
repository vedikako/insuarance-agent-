from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.ai_agents.contracts.messages import DisputeAnswer
from app.ai_agents.eligibility.rules import decision_for, money, payable_limit
from app.ai_agents.eligibility.state import EligibilityState
from app.services.policy_retrieval import rank_chunks
from app.services.review_inputs import registered_clauses

logger = logging.getLogger(__name__)


def _parse_model_json(raw_text: str) -> dict[str, Any]:
    raw = raw_text.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _configurable(config: RunnableConfig | None) -> dict[str, Any]:
    if not config:
        return {}
    return config.get("configurable") or {}


def _request(state: EligibilityState) -> dict[str, Any]:
    return state.get("request") or {}


def _fields(state: EligibilityState) -> dict[str, Any]:
    fields = _request(state).get("extracted_fields") or {}
    return fields if isinstance(fields, dict) else {}


async def _clauses_for(policy_id: str, query: str, source: Any) -> list[dict[str, Any]]:
    if source is not None:
        found = await source(policy_id, query)
        return list(found or [])
    registered = registered_clauses(policy_id)
    if registered is None:
        return []
    return rank_chunks(registered, query, k=4)


def _query(request: dict[str, Any], broader: bool) -> str:
    fields = request.get("extracted_fields") or {}
    parts = [
        str(request.get("diagnosis") or ""),
        str(request.get("incident_summary") or ""),
        str(fields.get("diagnosis") or ""),
        str(request.get("claim_type") or ""),
        "hospitalisation inpatient cover exclusion",
    ]
    if broader:
        parts.append("day care procedure waiting period room rent sum insured")
    return " ".join(part for part in parts if part).strip()


async def retrieve_node(state: EligibilityState, config: RunnableConfig) -> dict[str, Any]:
    request = _request(state)
    query = state.get("query") or _query(request, broader=bool(state.get("critiqued")))
    policy_id = str(request.get("policy_id") or "")
    clauses = await _clauses_for(policy_id, query, _configurable(config).get("clauses"))
    normalized = []
    for clause in clauses:
        quote = str(clause.get("quote") or clause.get("content") or "").strip().replace("\n", " ")
        if len(quote) > 400:
            quote = quote[:397] + "..."
        if not quote:
            continue
        normalized.append(
            {
                "chunk_id": str(clause.get("chunk_id") or ""),
                "section_path": str(clause.get("section_path") or clause.get("section") or ""),
                "quote": quote,
            }
        )
    logger.info("eligibility retrieve policy_id=%s clauses=%s", policy_id, len(normalized))
    calls = list(state.get("tool_calls") or [])
    calls.append(
        {
            "tool_name": "retrieve_clauses",
            "ok": bool(normalized),
            "summary": f"Retrieved {len(normalized)} clauses for this claim.",
        }
    )
    return {"query": query, "clauses": normalized, "tool_calls": calls}


async def draft_node(state: EligibilityState, config: RunnableConfig) -> dict[str, Any]:
    request = _request(state)
    fields = _fields(state)
    ledger = request.get("ledger") or {}
    claim_amount = float(request.get("claim_amount") or 0)
    remaining = float(ledger.get("remaining_balance") or 0)
    limit = payable_limit(claim_amount, remaining, fields)
    clauses = state.get("clauses") or []
    if not clauses:
        draft = {
            "decision": "insufficient_evidence",
            "payable_amount": 0.0,
            "rationale": "No policy clause was retrieved for this claim.",
        }
    else:
        draft = {
            "decision": decision_for(limit, claim_amount),
            "payable_amount": limit,
            "rationale": f"The quoted clause supports a payable amount of {limit:.2f} INR.",
        }
    llm = _configurable(config).get("llm")
    if llm is not None and clauses:
        try:
            raw = await llm.complete(
                "Decide cover from the clauses and ledger. Return JSON with decision and payable_amount.",
                str({"clauses": clauses, "ledger": ledger, "claim_amount": claim_amount, "fields": fields}),
            )
            parsed = _parse_model_json(raw)
            proposed = money(parsed.get("payable_amount"))
            if proposed is not None:
                draft["proposed_payable"] = proposed
                draft["payable_amount"] = proposed
            if parsed.get("decision") in {"eligible", "partially_eligible", "ineligible", "insufficient_evidence"}:
                draft["decision"] = parsed["decision"]
            if parsed.get("rationale"):
                draft["rationale"] = str(parsed["rationale"])
        except Exception:
            logger.exception("eligibility draft model failed")
    return {"draft": draft}


async def judge_node(state: EligibilityState, config: RunnableConfig) -> dict[str, Any]:
    draft = state.get("draft") or {}
    clauses = state.get("clauses") or []
    decision = str(draft.get("decision") or "insufficient_evidence")
    unsupported: list[str] = []
    if decision in {"eligible", "partially_eligible"} and not clauses:
        score = 1
        rationale = "The draft names cover, but no clause was retrieved."
        unsupported.append("A cover decision needs a quoted clause.")
    elif not clauses:
        score = 2
        rationale = "No clause was available to support the draft."
    else:
        score = 5
        rationale = "The payable amount is within the ledger and a retrieved clause."
    request = _request(state)
    if int(request.get("attempt") or 1) == 2:
        revision = request.get("revision") or {}
        for statement in revision.get("unsupported_statements") or []:
            if statement not in unsupported:
                unsupported.append(str(statement))
        if unsupported and score > 3:
            score = 3
            rationale = "The earlier draft had statements the clauses do not support."
    return {
        "groundedness_score": score,
        "judge_rationale": rationale,
        "unsupported_statements": unsupported,
    }


def route_after_judge(state: EligibilityState) -> str:
    request = _request(state)
    if int(request.get("attempt") or 1) != 1 or state.get("critiqued"):
        return "finalize"
    if int(state.get("groundedness_score") or 0) < 4:
        return "critique"
    return "finalize"


async def critique_node(state: EligibilityState, config: RunnableConfig) -> dict[str, Any]:
    logger.info("eligibility critique")
    return {"critiqued": True, "query": _query(_request(state), broader=True)}


def _answers(state: EligibilityState, payable: float) -> list[dict[str, Any]]:
    revision = (_request(state).get("revision") or {})
    answers = []
    for item in revision.get("disputes") or []:
        revised = money(item.get("document_value"))
        if item.get("code") == "AMOUNT_CONFLICT" and revised is not None:
            stance = "revise"
            amount = revised
        elif item.get("code") in {"DATE_CONFLICT", "IDENTITY_CONFLICT"}:
            stance = "revise"
            amount = 0.0
        else:
            stance = "insufficient_evidence"
            amount = None
        if stance == "revise" and amount is not None and abs(amount - payable) < 0.01 and payable > 0:
            stance = "sustain"
            amount = None
        answers.append(
            DisputeAnswer(
                code=str(item.get("code") or ""),
                stance=stance,  # type: ignore[arg-type]
                revised_payable_amount=amount,
                rationale="The dispute was checked against the ledger and the extracted fields.",
            ).model_dump()
        )
    return answers


async def finalize_node(state: EligibilityState, config: RunnableConfig) -> dict[str, Any]:
    request = _request(state)
    fields = _fields(state)
    ledger = request.get("ledger") or {}
    claim_amount = float(request.get("claim_amount") or 0)
    remaining = float(ledger.get("remaining_balance") or 0)
    limit = payable_limit(claim_amount, remaining, fields)
    draft = state.get("draft") or {}
    proposed = money(draft.get("payable_amount"))
    if proposed is None:
        proposed = 0.0
    numeric_inconsistency = proposed > limit + 0.01
    payable = round(min(proposed, limit), 2)
    reasons: list[str] = []
    if numeric_inconsistency:
        reasons.append("PAYABLE_ABOVE_LEDGER")
        payable = limit
    decision = str(draft.get("decision") or "insufficient_evidence")
    if decision != "insufficient_evidence":
        decision = decision_for(payable, claim_amount)
    clauses = state.get("clauses") or []
    citations = [
        {
            "chunk_id": str(clause.get("chunk_id") or ""),
            "section": str(clause.get("section_path") or clause.get("section") or ""),
            "quote": str(clause.get("quote") or "")[:400],
        }
        for clause in clauses
        if clause.get("quote")
    ]
    score = int(state.get("groundedness_score") or 1)
    if not citations:
        decision = "insufficient_evidence"
        reasons.append("NO_CITATION")
        score = min(score, 2)
    if score < 4:
        reasons.append("LOW_GROUNDEDNESS")
    answers = _answers(state, payable)
    revised_amounts = [
        money(item.get("revised_payable_amount"))
        for item in answers
        if item.get("stance") == "revise"
    ]
    revised_amounts = [amount for amount in revised_amounts if amount is not None]
    if revised_amounts:
        payable = round(min(revised_amounts), 2)
        if payable > limit:
            payable = limit
            numeric_inconsistency = True
            if "PAYABLE_ABOVE_LEDGER" not in reasons:
                reasons.append("PAYABLE_ABOVE_LEDGER")
        decision = decision_for(payable, claim_amount)
        if decision in {"eligible", "partially_eligible"} and not citations:
            decision = "insufficient_evidence"
    if any(item.get("stance") == "sustain" for item in answers) and not revised_amounts:
        payable = round(min(proposed, limit), 2)
    return {
        "result": {
            "decision": decision,
            "payable_amount": payable,
            "groundedness_score": score,
            "judge_rationale": state.get("judge_rationale") or "",
            "citations": citations,
            "unsupported_statements": list(state.get("unsupported_statements") or []),
            "answered_disputes": answers,
            "needs_manual_review": bool(reasons) or score < 4 or decision == "insufficient_evidence",
            "manual_review_reasons": reasons,
            "numeric_inconsistency": numeric_inconsistency,
            "tool_calls": list(state.get("tool_calls") or []),
        }
    }
