"""
REST API routes — Stage 7 + 8.

All routes are read-only from PostgreSQL (except simulation endpoints).
No business logic — agents compute everything, the API only serves what's stored.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import desc, func, select, update

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


# ── Crisis injector ───────────────────────────────────────────────────────────

@router.post("/simulation/{program_id}/inject-crisis")
async def inject_crisis(program_id: str):
    """
    POST /api/simulation/{program_id}/inject-crisis

    Injects a realistic crisis scenario:
    - 3 critical-path IN_PROGRESS tickets → BLOCKED with realistic blocker_ids
    - 2 of the 3 get stale_since = 4 days ago
    - Team with most IN_PROGRESS points gets +3 story points on 2 tickets (OVERLOADED)
    - Triggers one immediate pipeline cycle
    Returns ids of mutated tickets and the run_id.
    """
    import asyncio
    import json as _json
    import os
    import random
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    import redis.asyncio as aioredis

    from agents.runner import run_cycle
    from core.context_loader import load_context

    pid = _uuid(program_id)
    now = datetime.now(timezone.utc)
    stale_date = now - timedelta(days=4)

    async with AsyncSessionLocal() as db:
        # ── Load all non-DONE tickets ─────────────────────────────────────
        all_result = await db.execute(
            select(Ticket).where(Ticket.program_id == pid)
        )
        all_tickets = all_result.scalars().all()

    if not all_tickets:
        raise HTTPException(status_code=404, detail="No tickets found for this program")

    non_done = [t for t in all_tickets if t.status != "DONE"]
    in_progress = [t for t in non_done if t.status == "IN_PROGRESS"]

    if len(in_progress) < 3:
        # Reset some DONE tickets to IN_PROGRESS so the demo works even late in simulation
        done_crit = [t for t in all_tickets if t.status == "DONE" and t.is_on_critical_path][:3]
        targets = done_crit if len(done_crit) >= 3 else all_tickets[:3]
        target_ids = [t.id for t in targets]
    else:
        # Prefer critical-path IN_PROGRESS tickets
        crit_ip = [t for t in in_progress if t.is_on_critical_path]
        if len(crit_ip) >= 3:
            targets = random.sample(crit_ip, 3)
        else:
            targets = crit_ip + random.sample(
                [t for t in in_progress if not t.is_on_critical_path],
                min(3 - len(crit_ip), len(in_progress) - len(crit_ip))
            )
            targets = targets[:3]
        target_ids = [t.id for t in targets]

    # ── Find blocker candidates: other in-sprint tickets not being blocked ──
    blocker_pool = [
        t for t in non_done
        if t.id not in target_ids and t.status in ("IN_PROGRESS", "IN_REVIEW", "TODO")
    ]
    if len(blocker_pool) < 3:
        blocker_pool = [t for t in non_done if t.id not in target_ids]

    # Assign one realistic blocker per target ticket
    blocker_assignments = {}
    used_blockers = set()
    for t in targets:
        available = [b for b in blocker_pool if b.id not in used_blockers]
        if available:
            blocker = random.choice(available)
            blocker_assignments[t.id] = [blocker.id]
            used_blockers.add(blocker.id)
        else:
            blocker_assignments[t.id] = []

    # ── Stale: first 2 target tickets get stale_since ────────────────────
    stale_targets = [t.id for t in targets[:2]]
    injected_stale = stale_targets

    # ── Overloaded: find team with most IN_PROGRESS points ───────────────
    team_points: dict[str, int] = {}
    team_tickets: dict[str, list] = {}
    for t in in_progress:
        team = t.team or "Unknown"
        team_points[team] = team_points.get(team, 0) + (t.story_points or 0)
        team_tickets.setdefault(team, []).append(t)

    overload_targets = []
    injected_overload = []
    if team_points:
        busiest_team = max(team_points, key=team_points.get)
        team_tix = [t for t in team_tickets[busiest_team] if t.id not in target_ids]
        overload_targets = team_tix[:2]
        injected_overload = [t.id for t in overload_targets]

    # ── Apply all mutations to PostgreSQL ─────────────────────────────────
    async with AsyncSessionLocal() as db:
        for t in targets:
            values = {
                "status": "BLOCKED",
                "blocker_ids": blocker_assignments.get(t.id, []),
                "is_on_critical_path": True,
            }
            if t.id in stale_targets:
                values["stale_since"] = stale_date
            await db.execute(
                update(Ticket)
                .where(Ticket.id == t.id, Ticket.program_id == pid)
                .values(**values)
            )

        for t in overload_targets:
            await db.execute(
                update(Ticket)
                .where(Ticket.id == t.id, Ticket.program_id == pid)
                .values(story_points=(t.story_points or 3) + 3)
            )

        await db.commit()

    # ── Publish mutation events to Redis Stream ────────────────────────────
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url)
    try:
        crisis_events = []
        for t in targets:
            crisis_events.append({
                "type": "CRISIS_BLOCK",
                "ticket_id": t.id,
                "blocker_ids": _json.dumps(blocker_assignments.get(t.id, [])),
                "stale": "true" if t.id in stale_targets else "false",
                "timestamp": now.isoformat(),
            })
        for t in overload_targets:
            crisis_events.append({
                "type": "CRISIS_OVERLOAD",
                "ticket_id": t.id,
                "points_added": "3",
                "timestamp": now.isoformat(),
            })
        for evt in crisis_events:
            await r.xadd(
                f"streams:sim_events:{program_id}",
                evt,
                maxlen=500,
                approximate=True,
            )
    finally:
        await r.aclose()

    # ── Trigger immediate pipeline cycle ──────────────────────────────────
    DEFAULT_YAML = (
        Path(__file__).parent.parent.parent / "config" / "programs" / "default.yaml"
    )
    ctx = load_context(str(DEFAULT_YAML))
    cycle_result = await run_cycle(program_id, ctx)

    return {
        "injected_blocks":   target_ids,
        "injected_stale":    injected_stale,
        "injected_overload": injected_overload,
        "blocker_map":       blocker_assignments,
        "cycle_triggered":   True,
        "run_id":            cycle_result["run_id"],
        "cycle_number":      cycle_result["cycle_number"],
        "risk_flags_detected": len(cycle_result.get("risk_flags", [])),
    }
