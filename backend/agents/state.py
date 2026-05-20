"""
LangGraph pipeline state definition.

PipelineState is the single shared dict that flows through every agent node.
Each agent reads from it and returns partial updates that LangGraph merges back
in. All values must be JSON-serializable — no SQLAlchemy objects, no Pydantic
models, no datetime objects (use ISO strings instead).
"""
from __future__ import annotations

from typing import Dict, List

from typing_extensions import TypedDict


class PipelineState(TypedDict):
    # ── Cycle identity ────────────────────────────────────────────────────────
    run_id: str           # UUID4 string, unique per cycle
    cycle_number: int     # monotonically increasing per program
    started_at: str       # ISO-8601 UTC datetime string

    # ── Program config ────────────────────────────────────────────────────────
    program_id: str       # UUID string of the Program row
    program_context: dict  # ProgramContext serialized via model_dump(mode="json")

    # ── Agent outputs (populated as the pipeline progresses) ─────────────────
    tickets: List[dict]           # Telemetry Agent: all ticket dicts for this program
    sprint_summaries: List[dict]  # Telemetry Agent: per-sprint rollup
    risk_flags: List[dict]        # Risk Detection Agent: detected flags
    sprint_health: List[dict]     # Risk Detection Agent: health badge per sprint
    mitigations: List[dict]       # Mitigation Agent: rule-based actions
    executive_outputs: Dict       # Communication Agent: standup/escalation/digest

    # ── Cross-cutting (each agent may append; never replace the whole list) ───
    agent_decisions: List[dict]   # One entry per agent per cycle
    errors: List[dict]            # Non-fatal errors; loop continues on append
