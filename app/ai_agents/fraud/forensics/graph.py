from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.ai_agents.fraud.forensics.planner import planned_tools
from app.ai_agents.fraud.forensics.state import ForensicsState
from app.ai_agents.fraud.forensics.tools import CRITICAL_FLAGS, TOOLS, disputes_for
from app.services.review_inputs import read_document_bytes

logger = logging.getLogger(__name__)

_graph = None


def _configurable(config: RunnableConfig | None) -> dict[str, Any]:
    if not config:
        return {}
    return config.get("configurable") or {}


def _band(score: float) -> str:
    if score < 0.20:
        return "MINIMAL"
    if score < 0.40:
        return "LOW"
    if score < 0.60:
        return "MEDIUM"
    if score < 0.80:
        return "HIGH"
    return "VERY_HIGH"


async def plan_node(state: ForensicsState, config: RunnableConfig) -> dict[str, Any]:
    if "queue" not in state:
        return {"queue": list(state.get("queue") or []), "used": [], "observations": []}
    return {}


async def act_node(state: ForensicsState, config: RunnableConfig) -> dict[str, Any]:
    queue = list(state.get("queue") or [])
    if not queue:
        return {"done": True}
    tool_name = queue.pop(0)
    tool = TOOLS[tool_name]
    reader = _configurable(config).get("reader") or read_document_bytes
    logger.info("fraud tool %s", tool_name)
    try:
        if tool_name == "image_forensics":
            observation = await tool(state.get("request") or {}, reader)
        else:
            observation = tool(state.get("request") or {})
    except Exception:
        logger.exception("fraud tool failed name=%s", tool_name)
        observation = {
            "tool_name": tool_name,
            "ok": False,
            "score": 0,
            "flags": [],
            "evidence": "The tool failed before it could return data.",
            "details": {},
        }
    used = list(state.get("used") or [])
    used.append(tool_name)
    observations = list(state.get("observations") or [])
    observations.append(observation)
    return {"queue": queue, "used": used, "observations": observations, "done": not queue}


def route_after_act(state: ForensicsState) -> str:
    if state.get("done") or not state.get("queue"):
        return "synthesize"
    return "act"


async def synthesize_node(state: ForensicsState, config: RunnableConfig) -> dict[str, Any]:
    request = state.get("request") or {}
    observations = state.get("observations") or []
    required = [name for name in (state.get("used") or []) if name in {"arithmetic_gst", "cross_document", "image_forensics", "id_redaction"}]
    if required and observations and all(not item.get("ok") for item in observations if item.get("tool_name") in required):
        return {
            "result": {
                "status": "error",
                "error_code": "TOOL_FAILED",
                "error_message": "Every required fraud tool failed.",
                "observations": observations,
            }
        }
    flags: list[str] = []
    scores: list[float] = []
    evidence: list[str] = []
    for item in observations:
        flags.extend(str(flag) for flag in item.get("flags") or [])
        scores.append(float(item.get("score") or 0))
        if item.get("evidence"):
            evidence.append(str(item["evidence"]))
    flags = list(dict.fromkeys(flags))
    score = max(scores) if scores else 0.0
    critical = any(flag in CRITICAL_FLAGS for flag in flags)
    risk = _band(score)
    if critical and risk in {"MINIMAL", "LOW", "MEDIUM"}:
        risk = "HIGH"
    revision = request.get("revision") or {}
    question = str(revision.get("question") or "")
    if question:
        explanation = f"{question} {evidence[-1] if evidence else 'No further evidence was found.'}"
    elif evidence:
        explanation = " ".join(evidence)
    else:
        explanation = "No fraud flags on these documents."
    return {
        "result": {
            "status": "ok",
            "score": score,
            "risk_level": risk,
            "flags": flags,
            "critical": critical,
            "tools_used": list(state.get("used") or []),
            "observations": observations,
            "explanation": explanation,
            "disputes": disputes_for(request),
            "needs_manual_review": critical,
            "manual_review_reasons": flags if critical else [],
        }
    }


def build_forensics_graph():
    graph = StateGraph(ForensicsState)
    graph.add_node("plan", plan_node)
    graph.add_node("act", act_node)
    graph.add_node("synthesize", synthesize_node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "act")
    graph.add_conditional_edges("act", route_after_act, {"act": "act", "synthesize": "synthesize"})
    graph.add_edge("synthesize", END)
    return graph.compile()


def get_forensics_graph():
    global _graph
    if _graph is None:
        _graph = build_forensics_graph()
    return _graph
