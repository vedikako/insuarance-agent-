from __future__ import annotations

import json
import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

from langchain_core.runnables import RunnableConfig

from app.ai_agents.policy_qa.llm import JsonLLM, parse_json_object
from app.ai_agents.policy_qa.prompts import REWRITE_PROMPT, SUPERVISOR_PROMPT, SYNTHESIZER_PROMPT
from app.ai_agents.policy_qa.state import PolicyQAState
from app.ai_agents.policy_qa.tools import MAX_TOOL_CALLS, TOOL_NAMES, execute_tool, is_empty
from app.services.policy_data import PolicyDataPort

DIRECT_FALLBACK = "Hello. Ask about this policy's cover, or how much is left to claim."


def _configurable(config: RunnableConfig | None) -> dict[str, Any]:
    if not config:
        return {}
    return config.get("configurable") or {}


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
    user = (
        f"Recent turns:\n{_history_text(state.get('history') or [])}\n\n"
        f"Latest message:\n{state.get('message', '')}"
    )
    try:
        raw = await llm.complete(SUPERVISOR_PROMPT, user)
        decision = parse_json_object(raw)
    except Exception:
        logger.exception("Policy Q&A supervisor failed")
        return {
            "error_code": "MODEL_UNAVAILABLE",
            "error_message": "The supervisor could not decide a route.",
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
