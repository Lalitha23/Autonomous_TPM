"""
Telemetry Agent — Stage 4.

Reads the current snapshot of tickets and sprints from PostgreSQL, computes
sprint summaries (completion %, blocked count, stale count), identifies stale
ticket candidates by reading the stale_since field written by the Simulation
Engine, and persists one agent_decision row per cycle.

Design rules (from architecture):
- Never writes to the tickets or sprints tables (read-only).
- Identifies stale tickets by reading stale_since — does NOT write stale_since.
- Produces serializable dicts only; no SQLAlchemy objects leave this function.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List

from sqlalchemy import select

from agents.state import PipelineState
from db.models import AgentDecision, Sprint, Ticket
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def run_telemetry_agent(state: PipelineState) -> dict:
    """
    LangGraph node: Telemetry Agent.

    Args:
        state: Current PipelineState dict passed by LangGraph.

    Returns:
        Partial state update dict with keys: tickets, sprint_summaries,
        agent_decisions.
    """
    program_id_str = state["program_id"]
    cycle_number = state["cycle_number"]
    run_id = state["run_id"]
    ctx_dict = state["program_context"]

    stale_days: int = (
        ctx_dict.get("thresholds", {}).get("stale_ticket_days", 3)
    )
    domain: str = ctx_dict.get("domain", "unknown")
    program_uuid = uuid.UUID(program_id_str)
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        # ── Load sprints ──────────────────────────────────────────────────
        sprints_result = await session.execute(
            select(Sprint).where(Sprint.program_id == program_uuid)
        )
        sprints: List[Sprint] = sprints_result.scalars().all()

        # ── Load tickets ──────────────────────────────────────────────────
        tickets_result = await session.execute(
            select(Ticket).where(Ticket.program_id == program_uuid)
        )
        db_tickets: List[Ticket] = tickets_result.scalars().all()

    # ── Group tickets by sprint ───────────────────────────────────────────
    tickets_by_sprint: dict[str, list[Ticket]] = {}
    for t in db_tickets:
        sid = t.sprint_id or "unassigned"
        tickets_by_sprint.setdefault(sid, []).append(t)

    # ── Build sprint summaries ────────────────────────────────────────────
    sprint_summaries: List[dict] = []
    for sprint in sprints:
        sprint_tickets = tickets_by_sprint.get(sprint.id, [])
        planned_points = sum(t.story_points for t in sprint_tickets)
        completed_points = sum(t.points_completed for t in sprint_tickets)
        blocked_count = sum(1 for t in sprint_tickets if t.status == "BLOCKED")

        stale_count = 0
        for t in sprint_tickets:
            if t.stale_since is not None:
                stale_since = t.stale_since
                # Normalize to UTC-aware for safe arithmetic
                if stale_since.tzinfo is None:
                    stale_since = stale_since.replace(tzinfo=timezone.utc)
                if (now - stale_since).days >= stale_days:
                    stale_count += 1

        pct = (
            round(completed_points / planned_points * 100, 1)
            if planned_points > 0
            else 0.0
        )

        sprint_summaries.append({
            "sprint_id": sprint.id,
            "name": sprint.name,
            "planned_points": planned_points,
            "completed_points": completed_points,
            "pct_complete": pct,
            "ticket_count": len(sprint_tickets),
            "blocked_count": blocked_count,
            "stale_count": stale_count,
        })

    # ── Build serializable ticket list ────────────────────────────────────
    stale_candidate_ids: List[str] = []
    ticket_dicts: List[dict] = []

    for t in db_tickets:
        stale_candidate = False
        if t.stale_since is not None:
            stale_since = t.stale_since
            if stale_since.tzinfo is None:
                stale_since = stale_since.replace(tzinfo=timezone.utc)
            if (now - stale_since).days >= stale_days:
                stale_candidate = True
                stale_candidate_ids.append(t.id)

        ticket_dicts.append({
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "priority": t.priority,
            "assignee": t.assignee,
            "team": t.team,
            "sprint_id": t.sprint_id,
            "story_points": t.story_points,
            "points_completed": t.points_completed,
            "is_on_critical_path": t.is_on_critical_path,
            "blocker_ids": t.blocker_ids or [],
            "stale_since": (
                t.stale_since.isoformat() if t.stale_since else None
            ),
            "stale_candidate": stale_candidate,
            "scope_changed": t.scope_changed,
            "milestone_target": t.milestone_target,
            "risk_flag": t.risk_flag,
            "risk_severity": t.risk_severity,
            "risk_reason": t.risk_reason,
            "updated_at": (
                t.updated_at.isoformat() if t.updated_at else None
            ),
        })

    n_tickets = len(ticket_dicts)
    n_sprints = len(sprint_summaries)
    n_stale = len(stale_candidate_ids)

    # ── Build decision entry ──────────────────────────────────────────────
    sprint_brief = "; ".join(
        f"{s['name']}: {s['pct_complete']}% complete "
        f"({s['blocked_count']} blocked, {s['stale_count']} stale)"
        for s in sprint_summaries
    )

    decision_text = (
        f"Loaded {n_tickets} tickets across {n_sprints} sprints. "
        f"{n_stale} stale candidate{'s' if n_stale != 1 else ''} detected."
    )
    reasoning_text = (
        f"Sprint summaries: {sprint_brief if sprint_brief else 'none'}. "
        f"Stale candidates: "
        f"{stale_candidate_ids if stale_candidate_ids else 'none'}."
    )

    decision_dict = {
        "agent": "telemetry",
        "decision": decision_text,
        "reasoning": reasoning_text,
        "input_summary": {
            "program_id": program_id_str,
            "cycle_number": cycle_number,
        },
        "output_summary": {
            "ticket_count": n_tickets,
            "sprint_count": n_sprints,
            "stale_candidates": n_stale,
        },
    }

    # ── Persist decision to PostgreSQL ────────────────────────────────────
    async with AsyncSessionLocal() as session:
        session.add(
            AgentDecision(
                program_id=program_uuid,
                domain=domain,
                run_id=run_id,
                cycle_number=cycle_number,
                agent_name="telemetry",
                decision=decision_text,
                reasoning=reasoning_text,
                input_summary=decision_dict["input_summary"],
                output_summary=decision_dict["output_summary"],
            )
        )
        await session.commit()

    logger.info(
        "Telemetry [run=%s cycle=%d]: %d tickets, %d sprints, %d stale",
        run_id[:8],
        cycle_number,
        n_tickets,
        n_sprints,
        n_stale,
    )

    # ── Return partial state update ───────────────────────────────────────
    existing_decisions: List[dict] = list(state.get("agent_decisions", []))
    return {
        "tickets": ticket_dicts,
        "sprint_summaries": sprint_summaries,
        "agent_decisions": existing_decisions + [decision_dict],
    }
