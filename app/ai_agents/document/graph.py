from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.ai_agents.document.nodes import (
    extract_node,
    finalize_node,
    plan_node,
    reextract_node,
    route_after_validate,
    validate_node,
)
from app.ai_agents.document.state import DocumentState

_graph = None


def build_document_graph():
    graph = StateGraph(DocumentState)
    graph.add_node("plan", plan_node)
    graph.add_node("extract", extract_node)
    graph.add_node("validate", validate_node)
    graph.add_node("reextract", reextract_node)
    graph.add_node("finalize", finalize_node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "extract")
    graph.add_edge("extract", "validate")
    graph.add_conditional_edges(
        "validate",
        route_after_validate,
        {"reextract": "reextract", "finalize": "finalize"},
    )
    graph.add_edge("reextract", "extract")
    graph.add_edge("finalize", END)
    return graph.compile()


def get_document_graph():
    global _graph
    if _graph is None:
        _graph = build_document_graph()
    return _graph
