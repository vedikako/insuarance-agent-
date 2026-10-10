from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.ai_agents.fraud.nodes import review_node
from app.ai_agents.fraud.state import FraudState

_graph = None


def build_fraud_graph():
    graph = StateGraph(FraudState)
    graph.add_node("review", review_node)
    graph.add_edge(START, "review")
    graph.add_edge("review", END)
    return graph.compile()


def get_fraud_graph():
    global _graph
    if _graph is None:
        _graph = build_fraud_graph()
    return _graph
