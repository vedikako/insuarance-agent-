from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Any, Literal

logger = logging.getLogger(__name__)

from langchain_core.runnables import RunnableConfig

from app.ai_agents.policy_qa.llm import JsonLLM, parse_json_object
from app.ai_agents.policy_qa.prompts import REWRITE_PROMPT, SUPERVISOR_PROMPT, SYNTHESIZER_PROMPT
from app.ai_agents.policy_qa.state import PolicyQAState
from app.ai_agents.policy_qa.tools import MAX_TOOL_CALLS, TOOL_NAMES, execute_tool, is_empty
from app.services.policy_data import PolicyDataPort

DIRECT_FALLBACK = "Hello. Ask about this policy's cover, or how much is left to claim."
_MONEY = re.compile(
    r"how much|still claim|left to claim|remaining|balance|already claimed|sum insured|amount left",
    re.IGNORECASE,
)
_WORDING = re.compile(
    r"cover|exclusion|waiting|clause|wording|deductible|room rent|cataract",
    re.IGNORECASE,
)


def _configurable(config: RunnableConfig | None) -> dict[str, Any]:
    if not config:
        return {}
    return config.get("configurable") or {}


def _plain_amount(value: Any) -> str:
    number = Decimal(str(value))
    if number == number.to_integral_value():
        return str(number.quantize(Decimal("1")))
    return format(number.normalize(), "f")


def tools_for(message: str) -> list[str]:
    if _MONEY.search(message) and not _WORDING.search(message):
        return ["get_balance"]
    tools: list[str] = []
    if _MONEY.search(message):
        tools.append("get_balance")
    if _WORDING.search(message) or not tools:
        tools.append("retrieve_clauses")
    return tools


def reply_from_records(context: dict[str, Any]) -> str:
    balance = context.get("get_balance")
    if isinstance(balance, dict) and balance.get("remaining_balance") is not None:
        remaining = _plain_amount(balance["remaining_balance"])
        parts = [f"You can still claim {remaining} INR on this policy."]
        if balance.get("sum_insured") is not None and balance.get("utilized") is not None:
            insured = _plain_amount(balance["sum_insured"])
            used = _plain_amount(balance["utilized"])
            parts.append(f"The sum insured is {insured} INR and {used} INR has already been used.")
        return " ".join(parts)
    chunks = context.get("retrieve_clauses")
    if isinstance(chunks, list) and chunks:
        quote = str(chunks[0].get("quote") or "").strip()
        if quote:
            return f"From the policy wording: {quote}"
    return ""


def _history_text(history: list[dict[str, Any]]) -> str:
    lines = []
    for turn in history[-10:]:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def supervisor_node(state: PolicyQAState, config: RunnableConfig) -> dict[str, Any]:
    llm: JsonLLM = _configurable(config)["llm"]
    logger.info("supervisor start")
    message = state.get("message") or ""
    if _MONEY.search(message) and not _WORDING.search(message):
        logger.info("supervisor route=data tools=['get_balance'] source=ledger")
        return {
            "route": "data",
            "tools_needed": ["get_balance"],
            "supervisor_reason": "The question asks for an amount stored on the ledger.",
            "direct_reply": "",
            "skip_model_reply": True,
        }
    user = (
        f"Recent turns:\n{_history_text(state.get('history') or [])}\n\n"
        f"Latest message:\n{message}"
    )
    try:
        raw = await llm.complete(SUPERVISOR_PROMPT, user)
        decision = parse_json_object(raw)
    except Exception:
        logger.exception("Policy Q&A supervisor failed")
        tools = tools_for(message)
        logger.info("supervisor route=data tools=%s source=fallback", tools)
        return {
            "route": "data",
            "tools_needed": tools,
            "supervisor_reason": "The model did not answer, so the question was sent to the policy records.",
            "direct_reply": "",
        }
    route = str(decision.get("route") or "").lower()
    if route in {"data_node", "data"}:
        route = "data"
    elif route != "direct":
        route = "data"
    tools = [name for name in decision.get("tools_needed") or [] if name in TOOL_NAMES]
    direct_reply = str(decision.get("direct_reply") or "").strip()
    if route == "direct" and not direct_reply:
        direct_reply = DIRECT_FALLBACK
    logger.info("supervisor route=%s tools=%s", route, tools)
    return {
        "route": route,
        "tools_needed": tools,
        "supervisor_reason": str(decision.get("reason") or ""),
        "direct_reply": direct_reply if route == "direct" else "",
    }


def route_after_supervisor(state: PolicyQAState) -> Literal["data", "end"]:
    if state.get("error_code"):
        return "end"
    if state.get("route") == "data":
        return "data"
    return "end"


async def _rewrite_query(llm: JsonLLM, message: str) -> str:
    try:
        raw = await llm.complete(REWRITE_PROMPT, message)
        parsed = parse_json_object(raw)
        rewritten = str(parsed.get("query") or "").strip()
        if rewritten:
            return rewritten
    except Exception:
        pass
    return f"{message} coverage exclusion waiting period limit"


async def data_node(state: PolicyQAState, config: RunnableConfig) -> dict[str, Any]:
    logger.info("data start tools=%s", state.get("tools_needed"))
    conf = _configurable(config)
    port: PolicyDataPort = conf["deps"]
    llm: JsonLLM = conf["llm"]
    policy_id = state["policy_id"]
    message = state.get("message") or ""
    needed = [name for name in state.get("tools_needed") or [] if name in TOOL_NAMES]
    if not needed:
        needed = ["get_policy_facts"]

    context: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    clause_retried = False
    extra_added = False
    index = 0
    while index < len(needed) and len(records) < MAX_TOOL_CALLS:
        name = needed[index]
        index += 1
        query = message
        payload, record = await execute_tool(name, port, policy_id, query)
        records.append(record)
        context[name] = payload
        if (
            name == "retrieve_clauses"
            and is_empty(payload)
            and not clause_retried
            and len(records) < MAX_TOOL_CALLS
        ):
            clause_retried = True
            query = await _rewrite_query(llm, message)
            payload, record = await execute_tool(name, port, policy_id, query)
            records.append(record)
            context[name] = payload
        elif is_empty(payload) and not extra_added and len(records) < MAX_TOOL_CALLS:
            extra = next((tool for tool in TOOL_NAMES if tool not in needed), None)
            if extra:
                needed.append(extra)
                extra_added = True

    failed = [rec for rec in records if not rec["ok"]]
    update: dict[str, Any] = {"data_context": context, "tool_calls": records}
    if records and len(failed) == len(records):
        update["error_code"] = "TOOL_FAILED"
        update["error_message"] = "Every policy lookup failed."
    logger.info("data done tools=%s", [rec["tool_name"] for rec in records])
    return update


async def synthesizer_node(state: PolicyQAState, config: RunnableConfig) -> dict[str, Any]:
    if state.get("error_code") == "TOOL_FAILED":
        return {"reply": "I could not read this policy's records just now."}
    context = state.get("data_context") or {}
    if state.get("skip_model_reply"):
        reply = reply_from_records(context)
        if reply:
            logger.info("synthesizer done source=records chars=%s", len(reply))
            return {"reply": reply, "cited_chunk_ids": []}
    logger.info("synthesizer start")
    llm: JsonLLM = _configurable(config)["llm"]
    user = json.dumps(
        {
            "message": state.get("message"),
            "history": state.get("history") or [],
            "data_context": state.get("data_context") or {},
        },
        default=str,
    )
    try:
        raw = await llm.complete(SYNTHESIZER_PROMPT, user)
        parsed = parse_json_object(raw)
    except Exception:
        logger.exception("Policy Q&A synthesizer failed")
        reply = reply_from_records(state.get("data_context") or {})
        if reply:
            return {"reply": reply, "cited_chunk_ids": []}
        return {
            "error_code": "MODEL_UNAVAILABLE",
            "error_message": "The synthesizer could not write a reply.",
            "reply": "",
        }
    reply = str(parsed.get("reply") or "").strip()
    cited = parsed.get("cited_chunk_ids") or []
    if not isinstance(cited, list):
        cited = []
    logger.info("synthesizer done chars=%s", len(reply))
    return {"reply": reply, "cited_chunk_ids": [str(item) for item in cited]}
