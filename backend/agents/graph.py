"""
LangGraph pipeline graph — Stage 6 final.

Full pipeline:
  START → telemetry → risk_detection → dependency_analysis
        → mitigation → communication → END

All five agents are fully implemented.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agents.communication_agent import run_communication_agent
from agents.dependency_analysis_agent import run_dependency_analysis_agent
from agents.mitigation_agent import run_mitigation_agent
from agents.risk_detection_agent import run_risk_detection_agent
from agents.state import PipelineState
from agents.telemetry_agent import run_telemetry_agent


# ── Graph assembly ────────────────────────────────────────────────────────────

def _build_graph():
    graph = StateGraph(PipelineState)

    # Nodes
    graph.add_node("telemetry",            run_telemetry_agent)
    graph.add_node("risk_detection",       run_risk_detection_agent)
    graph.add_node("dependency_analysis",  run_dependency_analysis_agent)
    graph.add_node("mitigation",           run_mitigation_agent)
    graph.add_node("communication",        run_communication_agent)

    # Edges
    graph.add_edge(START,                  "telemetry")
    graph.add_edge("telemetry",            "risk_detection")
    graph.add_edge("risk_detection",       "dependency_analysis")
    graph.add_edge("dependency_analysis",  "mitigation")
    graph.add_edge("mitigation",           "communication")
    graph.add_edge("communication",        END)

    return graph.compile()


# Module-level compiled graph — imported by runner.py
compiled_graph = _build_graph()
