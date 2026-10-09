from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.ai_agents.policy_qa.nodes import (
    data_node,
    route_after_supervisor,
    supervisor_node,
    synthesizer_node,
)
from app.ai_agents.policy_qa.state import PolicyQAState

_graph = None


def build_policy_qa_graph():
    graph = StateGraph(PolicyQAState)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("data", data_node)
    graph.add_node("synthesizer", synthesizer_node)
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {"data": "data", "end": END},
    )
    graph.add_edge("data", "synthesizer")
    graph.add_edge("synthesizer", END)
    return graph.compile()


def get_policy_qa_graph():
    global _graph
    if _graph is None:
        _graph = build_policy_qa_graph()
    return _graph
