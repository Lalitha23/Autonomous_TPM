"""
LangGraph pipeline graph — Stage 5 update.

Full pipeline:
  START → telemetry → risk_detection → dependency_analysis
        → mitigation (stub) → communication (stub) → END

Telemetry, Risk Detection, and Dependency Analysis are fully implemented.
Mitigation and Communication remain pass-through stubs until Stage 6-7.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agents.dependency_analysis_agent import run_dependency_analysis_agent
from agents.risk_detection_agent import run_risk_detection_agent
from agents.state import PipelineState
from agents.telemetry_agent import run_telemetry_agent


# ── Remaining stub nodes ──────────────────────────────────────────────────────

async def _stub_mitigation(state: PipelineState) -> dict:
    """Stage 6 placeholder: Mitigation Agent."""
    return {}


async def _stub_communication(state: PipelineState) -> dict:
    """Stage 7 placeholder: Communication Agent."""
    return {}


# ── Graph assembly ────────────────────────────────────────────────────────────

def _build_graph():
    graph = StateGraph(PipelineState)

    # Nodes
    graph.add_node("telemetry",            run_telemetry_agent)
    graph.add_node("risk_detection",       run_risk_detection_agent)
    graph.add_node("dependency_analysis",  run_dependency_analysis_agent)
    graph.add_node("mitigation",           _stub_mitigation)
    graph.add_node("communication",        _stub_communication)

    # Edges
    graph.add_edge(START,                 "telemetry")
    graph.add_edge("telemetry",           "risk_detection")
    graph.add_edge("risk_detection",      "dependency_analysis")
    graph.add_edge("dependency_analysis", "mitigation")
    graph.add_edge("mitigation",          "communication")
    graph.add_edge("communication",        END)

    return graph.compile()


# Module-level compiled graph — imported by runner.py
compiled_graph = _build_graph()
