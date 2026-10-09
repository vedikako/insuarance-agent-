"""Policy Q&A runner.

Graph: supervisor -> END, or supervisor -> data -> synthesizer -> END.
The supervisor is the only node that chooses the route.
"""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.ai_agents.contracts.errors import AgentError
from app.ai_agents.contracts.messages import ClauseHit, PolicyQARequest, PolicyQAResult, ToolCallRecord
from app.ai_agents.policy_qa.graph import get_policy_qa_graph
from app.ai_agents.policy_qa.llm import JsonLLM
from app.ai_agents.policy_qa.state import PolicyQAState
from app.services.policy_data import PolicyDataPort

LEDGER_ONLY = "I can only state amounts that are stored on this policy's ledger."
NOT_FOUND = "I could not find that wording in this policy's documents."
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _normalize_amount(raw: str) -> Decimal | None:
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def extract_amounts(text: str) -> set[Decimal]:
    cleaned = _DATE.sub(" ", text)
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", cleaned)
    found: set[Decimal] = set()
    for match in _NUMBER.findall(cleaned):
        number = _normalize_amount(match)
        if number is None:
            continue
        if number >= 10 or "." in match:
            found.add(number)
    return found


def _walk_amounts(value: Any, found: set[Decimal]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float, Decimal)):
        found.add(Decimal(str(value)))
        return
    if isinstance(value, str):
        found.update(extract_amounts(value))
        return
    if isinstance(value, dict):
        for item in value.values():
            _walk_amounts(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_amounts(item, found)


def allowed_amounts(data_context: dict[str, Any], quotes: list[str]) -> set[Decimal]:
    found: set[Decimal] = set()
    for key in ("get_balance", "get_policy_facts"):
        if key in data_context:
            _walk_amounts(data_context[key], found)
    for quote in quotes:
        found.update(extract_amounts(quote))
    return found


def enforce_amount_rule(reply: str, data_context: dict[str, Any], quotes: list[str]) -> str:
    allowed = allowed_amounts(data_context, quotes)
    for amount in extract_amounts(reply):
        if amount not in allowed:
            return LEDGER_ONLY
    return reply


def _quote_fragment(quote: str) -> str:
    compact = " ".join(quote.split())
    return compact[:60]


def attach_wording(reply: str, chunks: list[dict[str, Any]] | None, cited_ids: list[str]) -> tuple[str, list[ClauseHit]]:
    if chunks is None:
        return reply, []
    if not chunks:
        if "could not find" not in reply.lower() and "not found" not in reply.lower():
            reply = f"{reply.rstrip()}\n\n{NOT_FOUND}".strip()
        return reply, []

    hits = []
    for chunk in chunks:
        fragment = _quote_fragment(str(chunk.get("quote") or ""))
        if fragment and fragment in " ".join(reply.split()):
            hits.append(chunk)
    if not hits and cited_ids:
        hits = [chunk for chunk in chunks if str(chunk.get("chunk_id")) in cited_ids]
    if not hits:
        first = chunks[0]
        reply = f"{reply.rstrip()}\n\nFrom the policy wording: {first.get('quote', '')}".strip()
        hits = [first]
    citations = [
        ClauseHit(
            chunk_id=str(chunk["chunk_id"]),
            section_path=str(chunk.get("section_path") or ""),
            quote=str(chunk.get("quote") or "")[:400],
        )
        for chunk in hits
    ]
    return reply, citations


def _error_result(route: str, code: str, message: str, retryable: bool) -> PolicyQAResult:
    return PolicyQAResult(
        status="error",
        route="data" if route == "data" else "direct",
        reply="",
        error=AgentError(code=code, message=message, retryable=retryable),  # type: ignore[arg-type]
    )


async def run_policy_qa(
    request: PolicyQARequest,
    *,
    deps: PolicyDataPort,
    llm: JsonLLM,
    timeout_seconds: float = 45,
) -> PolicyQAResult:
    graph = get_policy_qa_graph()
    initial: PolicyQAState = {
        "message": request.message,
        "history": list(request.history[-10:]),
        "policy_id": request.policy_id,
        "tool_calls": [],
        "data_context": {},
    }
    try:
        final = await asyncio.wait_for(
            graph.ainvoke(
                initial,
                config={"configurable": {"deps": deps, "llm": llm}},
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        return _error_result("direct", "TIMEOUT", "The policy assistant timed out.", True)

    route = final.get("route") or "direct"
    if route not in {"direct", "data"}:
        route = "direct"
    error_code = final.get("error_code")
    if error_code:
        return _error_result(
            route,
            error_code,
            final.get("error_message") or "The policy assistant failed.",
            error_code in {"TIMEOUT", "MODEL_UNAVAILABLE"},
        )

    if route == "direct":
        reply = enforce_amount_rule(final.get("direct_reply") or "", {}, [])
        return PolicyQAResult(status="ok", route="direct", reply=reply, tool_calls=[], citations=[], balance=None)

    context = final.get("data_context") or {}
    chunks = context.get("retrieve_clauses") if "retrieve_clauses" in context else None
    if chunks is not None and not isinstance(chunks, list):
        chunks = []
    reply, citations = attach_wording(
        final.get("reply") or "",
        chunks,
        final.get("cited_chunk_ids") or [],
    )
    quotes = [item.quote for item in citations]
    if isinstance(chunks, list):
        quotes.extend(str(chunk.get("quote") or "") for chunk in chunks)
    reply = enforce_amount_rule(reply, context, quotes)
    balance = context.get("get_balance")
    if not isinstance(balance, dict) or not balance:
        balance = None
    tool_calls = [ToolCallRecord(**record) for record in final.get("tool_calls") or []]
    return PolicyQAResult(
        status="ok",
        route="data",
        reply=reply,
        citations=citations,
        balance=balance,
        tool_calls=tool_calls,
    )
