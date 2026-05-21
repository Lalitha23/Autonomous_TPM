"""
REST API routes — Stage 7.

All routes are read-only from PostgreSQL. No business logic — agents
compute everything, the API only serves what's already stored.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import desc, func, select

from db.models import AgentDecision, ExecutiveOutput, Program, Sprint, Ticket
from db.session import AsyncSessionLocal

router = APIRouter(prefix="/api")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uuid(program_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(program_id)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid UUID: {program_id}")


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


# ── Programs ──────────────────────────────────────────────────────────────────

@router.get("/programs")
async def list_programs():
    """GET /api/programs — returns all active programs."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Program).where(Program.is_active == True).order_by(Program.created_at)
        )
        programs = result.scalars().all()

    return [
        {
            "id": str(p.id),
            "name": p.name,
            "domain": p.domain,
            "is_active": p.is_active,
            "created_at": _iso(p.created_at),
        }
        for p in programs
    ]


@router.get("/programs/{program_id}")
async def get_program(program_id: str):
    """GET /api/programs/{program_id} — single program with full context_config."""
    pid = _uuid(program_id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Program).where(Program.id == pid))
        program = result.scalar_one_or_none()

    if program is None:
        raise HTTPException(status_code=404, detail="Program not found")

    return {
        "id": str(program.id),
        "name": program.name,
        "domain": program.domain,
        "context_config": program.context_config,
        "is_active": program.is_active,
        "created_at": _iso(program.created_at),
    }


# ── Sprints ───────────────────────────────────────────────────────────────────

@router.get("/programs/{program_id}/sprints")
async def get_sprints(program_id: str):
    """
    GET /api/programs/{program_id}/sprints
    pct_complete is computed from ticket story_points in real time.
    """
    pid = _uuid(program_id)

    async with AsyncSessionLocal() as db:
        sprints_result = await db.execute(
            select(Sprint).where(Sprint.program_id == pid)
        )
        sprints = sprints_result.scalars().all()

        if not sprints:
            raise HTTPException(status_code=404, detail="Program not found or has no sprints")

        agg_result = await db.execute(
            select(
                Ticket.sprint_id,
                func.count(Ticket.id).label("ticket_count"),
                func.coalesce(func.sum(Ticket.story_points), 0).label("total_points"),
                func.coalesce(func.sum(Ticket.points_completed), 0).label("completed_points"),
            )
            .where(Ticket.program_id == pid)
            .group_by(Ticket.sprint_id)
        )
        agg_by_sprint = {row.sprint_id: row for row in agg_result}

    out = []
    for s in sprints:
        agg = agg_by_sprint.get(s.id)
        total = int(agg.total_points) if agg else 0
        completed = int(agg.completed_points) if agg else 0
        pct = round((completed / total * 100) if total > 0 else 0, 1)
        out.append({
            "sprint_id": s.id,
            "name": s.name,
            "pct_complete": pct,
            "health_badge": s.health_badge,
            "worst_severity": s.worst_severity,
            "ticket_count": int(agg.ticket_count) if agg else 0,
            "run_id": s.last_run_id,
            "updated_at": _iso(s.updated_at),
        })

    return out


# ── Tickets ───────────────────────────────────────────────────────────────────

@router.get("/programs/{program_id}/tickets")
async def get_tickets(
    program_id: str,
    team: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    flag: Optional[str] = Query(None),
):
    """
    GET /api/programs/{program_id}/tickets
    Optional filters: team, severity (LOW|MEDIUM|HIGH|CRITICAL), flag (STALE|BLOCKED|…)
    """
    pid = _uuid(program_id)

    async with AsyncSessionLocal() as db:
        q = select(Ticket).where(Ticket.program_id == pid)
        if team:
            q = q.where(Ticket.team == team)
        if severity:
            q = q.where(Ticket.risk_severity == severity.upper())
        if flag:
            q = q.where(Ticket.risk_flag == flag.upper())
        q = q.order_by(Ticket.sprint_id, Ticket.priority, Ticket.id)
        result = await db.execute(q)
        tickets = result.scalars().all()

    return [
        {
            "id": t.id,
            "title": t.title,
            "assignee": t.assignee,
            "team": t.team,
            "status": t.status,
            "priority": t.priority,
            "sprint_id": t.sprint_id,
            "story_points": t.story_points,
            "points_completed": t.points_completed,
            "is_on_critical_path": t.is_on_critical_path,
            "risk_flag": t.risk_flag,
            "risk_severity": t.risk_severity,
            "risk_reason": t.risk_reason,
            "updated_at": _iso(t.updated_at),
        }
        for t in tickets
    ]


# ── Agent Decisions ───────────────────────────────────────────────────────────

@router.get("/programs/{program_id}/decisions")
async def get_decisions(
    program_id: str,
    run_id: Optional[str] = Query(None),
    agent_name: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    GET /api/programs/{program_id}/decisions
    Paginated agent decision log, newest first.
    """
    pid = _uuid(program_id)

    async with AsyncSessionLocal() as db:
        base_q = select(AgentDecision).where(AgentDecision.program_id == pid)
        if run_id:
            base_q = base_q.where(AgentDecision.run_id == run_id)
        if agent_name:
            base_q = base_q.where(AgentDecision.agent_name == agent_name)

        count_q = select(func.count()).select_from(base_q.subquery())
        total = (await db.execute(count_q)).scalar_one()

        items_q = base_q.order_by(desc(AgentDecision.created_at)).offset(offset).limit(limit)
        result = await db.execute(items_q)
        decisions = result.scalars().all()

    return {
        "total": total,
        "items": [
            {
                "id": str(d.id),
                "agent_name": d.agent_name,
                "decision": d.decision,
                "reasoning": d.reasoning,
                "input_summary": d.input_summary,
                "output_summary": d.output_summary,
                "created_at": _iso(d.created_at),
            }
            for d in decisions
        ],
    }


# ── Executive Outputs ─────────────────────────────────────────────────────────

_OUTPUT_TYPE_MAP = {
    "standup_summary": "STANDUP_SUMMARY",
    "escalation_memo": "ESCALATION_MEMO",
    "risk_digest":     "RISK_DIGEST",
}


@router.get("/programs/{program_id}/outputs")
async def get_outputs(program_id: str):
    """
    GET /api/programs/{program_id}/outputs
    Latest of each output type: standup_summary, escalation_memo (nullable), risk_digest.
    """
    pid = _uuid(program_id)

    async with AsyncSessionLocal() as db:
        result: dict[str, dict | None] = {}
        for key, db_type in _OUTPUT_TYPE_MAP.items():
            row_result = await db.execute(
                select(ExecutiveOutput)
                .where(
                    ExecutiveOutput.program_id == pid,
                    ExecutiveOutput.output_type == db_type,
                )
                .order_by(desc(ExecutiveOutput.created_at))
                .limit(1)
            )
            row = row_result.scalar_one_or_none()
            result[key] = (
                {
                    "content": row.content,
                    "cycle_number": row.cycle_number,
                    "created_at": _iso(row.created_at),
                }
                if row
                else None
            )

    return result


@router.get("/programs/{program_id}/outputs/{output_type}")
async def get_output_by_type(program_id: str, output_type: str):
    """
    GET /api/programs/{program_id}/outputs/{output_type}
    output_type: standup_summary | escalation_memo | risk_digest
    """
    pid = _uuid(program_id)
    db_type = _OUTPUT_TYPE_MAP.get(output_type.lower())
    if db_type is None:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid output_type. Must be one of: {list(_OUTPUT_TYPE_MAP.keys())}",
        )

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ExecutiveOutput)
            .where(
                ExecutiveOutput.program_id == pid,
                ExecutiveOutput.output_type == db_type,
            )
            .order_by(desc(ExecutiveOutput.created_at))
            .limit(1)
        )
        row = result.scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail=f"No {output_type} found for this program")

    return {
        "content": row.content,
        "cycle_number": row.cycle_number,
        "run_id": row.run_id,
        "created_at": _iso(row.created_at),
    }


# ── Simulation trigger ────────────────────────────────────────────────────────

@router.post("/simulation/{program_id}/trigger")
async def trigger_cycle(program_id: str):
    """
    POST /api/simulation/{program_id}/trigger
    Fires one pipeline cycle immediately. Returns immediately; cycle runs in background.
    """
    import asyncio
    from datetime import datetime, timezone
    from pathlib import Path

    from agents.runner import run_cycle
    from core.context_loader import load_context

    _uuid(program_id)  # validate UUID

    DEFAULT_YAML = (
        Path(__file__).parent.parent.parent / "config" / "programs" / "default.yaml"
    )
    ctx = load_context(str(DEFAULT_YAML))

    async def _run():
        import logging
        try:
            await run_cycle(program_id, ctx)
        except Exception:
            logging.getLogger(__name__).exception("Manual trigger cycle failed.")

    asyncio.create_task(_run())

    return {
        "status": "triggered",
        "program_id": program_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "message": "Cycle triggered — check /api/programs/{program_id}/decisions for results",
    }
