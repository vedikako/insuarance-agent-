from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.ai_agents.settlement.nodes import assess_node
from app.ai_agents.settlement.state import SettlementState

_graph = None


def build_settlement_graph():
    graph = StateGraph(SettlementState)
    graph.add_node("assess", assess_node)
    graph.add_edge(START, "assess")
    graph.add_edge("assess", END)
    return graph.compile()


def get_settlement_graph():
    global _graph
    if _graph is None:
        _graph = build_settlement_graph()
    return _graph
