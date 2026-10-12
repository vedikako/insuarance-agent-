from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.ai_agents.eligibility.nodes import (
    critique_node,
    draft_node,
    finalize_node,
    judge_node,
    retrieve_node,
    route_after_judge,
)
from app.ai_agents.eligibility.state import EligibilityState

_graph = None


def build_eligibility_graph():
    graph = StateGraph(EligibilityState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("draft", draft_node)
    graph.add_node("judge", judge_node)
    graph.add_node("critique", critique_node)
    graph.add_node("finalize", finalize_node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "draft")
    graph.add_edge("draft", "judge")
    graph.add_conditional_edges(
        "judge",
        route_after_judge,
        {"critique": "critique", "finalize": "finalize"},
    )
    graph.add_edge("critique", "retrieve")
    graph.add_edge("finalize", END)
    return graph.compile()


def get_eligibility_graph():
    global _graph
    if _graph is None:
        _graph = build_eligibility_graph()
    return _graph
