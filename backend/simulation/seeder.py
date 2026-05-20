"""
Simulation Seeder — bootstraps PostgreSQL and Redis with initial backlog state.

Responsibilities:
1. Insert a Program row if one doesn't exist for the given program_id slug.
2. Call generate_initial_state() to get SimSprint / SimTicket objects.
3. Persist all sprints and tickets to PostgreSQL.
4. Pre-populate Redis cache keys so the dashboard renders on first load
   without waiting for the first 30-second agent cycle.

Redis cache keys written:
  - tickets:current:{program_id}    → JSON list of all ticket dicts   (TTL 35s)
  - sprint_health:current:{program_id} → JSON list of bare sprint dicts (TTL 35s)

This module is called once at startup, before the agent loop begins.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import List, Optional

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from core.program_context import ProgramContext
from db.models import Program, Sprint, Ticket
from simulation.engine import generate_initial_state
from simulation.models import SimSprint

logger = logging.getLogger(__name__)

_CACHE_TTL = 35  # seconds — matches REDIS_CACHE_TTL_SECONDS env var default


# ── Internal helpers ─────────────────────────────────────────────────────────

def _sprint_to_dict(sprint: SimSprint) -> dict:
    """Minimal sprint dict for Redis cache (no tickets embedded)."""
    return {
        "id": sprint.id,
        "name": sprint.name,
        "start_date": sprint.start_date.isoformat(),
        "end_date": sprint.end_date.isoformat(),
        "ticket_count": len(sprint.tickets),
        "total_points": sprint.total_points,
        "completed_points": sprint.completed_points,
    }


def _ticket_to_dict(ticket, sprint_id: str) -> dict:
    """Flat ticket dict for Redis cache."""
    return {
        "id": ticket.id,
        "title": ticket.title,
        "status": ticket.status,
        "priority": ticket.priority,
        "assignee": ticket.assignee,
        "team": ticket.team,
        "sprint_id": sprint_id,
        "story_points": ticket.story_points,
        "points_completed": ticket.points_completed,
        "is_on_critical_path": ticket.is_on_critical_path,
        "blocker_ids": ticket.blocker_ids,
        "stale_since": ticket.stale_since.isoformat() if ticket.stale_since else None,
        "scope_changed": ticket.scope_changed,
        "milestone_target": ticket.milestone_target,
        "risk_flag": ticket.risk_flag,
        "risk_severity": ticket.risk_severity,
        "risk_reason": ticket.risk_reason,
        "updated_at": ticket.updated_at.isoformat(),
    }


# ── Public API ───────────────────────────────────────────────────────────────

async def seed_program(
    db: AsyncSession,
    redis_client: aioredis.Redis,
    ctx: ProgramContext,
    rng_seed: Optional[int] = None,
) -> uuid.UUID:
    """
    Seed the database and Redis cache for a program.

    Idempotent: if a Program row with the matching program_id slug already
    exists, the function returns its UUID without re-seeding.

    Args:
        db:           Open async SQLAlchemy session.
        redis_client: Open async Redis client.
        ctx:          Loaded ProgramContext.
        rng_seed:     Optional seed for reproducible ticket generation (tests).

    Returns:
        UUID of the seeded (or pre-existing) Program row.
    """
    # ── 1. Upsert Program row ─────────────────────────────────────────────
    stmt = select(Program).where(Program.name == ctx.program_name)
    result = await db.execute(stmt)
    existing: Optional[Program] = result.scalar_one_or_none()

    if existing is not None:
        logger.info(
            "Program '%s' already seeded (id=%s). Skipping.",
            ctx.program_name,
            existing.id,
        )
        return existing.id

    program_uuid = uuid.uuid4()
    program = Program(
        id=program_uuid,
        name=ctx.program_name,
        domain=ctx.domain,
        context_config=ctx.model_dump(mode="json"),
        is_active=True,
    )
    db.add(program)
    await db.flush()  # get the row into the session without committing yet

    logger.info("Created Program row id=%s for '%s'", program_uuid, ctx.program_name)

    # ── 2. Generate initial backlog ───────────────────────────────────────
    sprints: List[SimSprint] = generate_initial_state(ctx, seed=rng_seed)

    # ── 3. Persist Sprints + Tickets to PostgreSQL ─────────────────────────
    all_ticket_dicts: List[dict] = []

    for sim_sprint in sprints:
        sprint_row = Sprint(
            id=sim_sprint.id,
            program_id=program_uuid,
            name=sim_sprint.name,
            start_date=sim_sprint.start_date,
            end_date=sim_sprint.end_date,
            # health_badge / worst_severity / last_run_id are NULL until first cycle
        )
        db.add(sprint_row)

        for sim_ticket in sim_sprint.tickets:
            ticket_row = Ticket(
                id=sim_ticket.id,
                program_id=program_uuid,
                title=sim_ticket.title,
                status=sim_ticket.status,
                priority=sim_ticket.priority,
                assignee=sim_ticket.assignee,
                team=sim_ticket.team,
                sprint_id=sim_sprint.id,
                story_points=sim_ticket.story_points,
                points_completed=sim_ticket.points_completed,
                is_on_critical_path=sim_ticket.is_on_critical_path,
                blocker_ids=sim_ticket.blocker_ids,
                stale_since=sim_ticket.stale_since,
                scope_changed=sim_ticket.scope_changed,
                milestone_target=sim_ticket.milestone_target,
                risk_flag=sim_ticket.risk_flag,
                risk_severity=sim_ticket.risk_severity,
                risk_reason=sim_ticket.risk_reason,
                updated_at=sim_ticket.updated_at,
                created_at=sim_ticket.created_at,
            )
            db.add(ticket_row)
            all_ticket_dicts.append(_ticket_to_dict(sim_ticket, sim_sprint.id))

    await db.commit()
    logger.info(
        "Seeded %d sprints and %d tickets for program '%s'.",
        len(sprints),
        len(all_ticket_dicts),
        ctx.program_name,
    )

    # ── 4. Pre-populate Redis cache ────────────────────────────────────────
    program_id_str = str(program_uuid)

    tickets_key = f"tickets:current:{program_id_str}"
    sprint_key = f"sprint_health:current:{program_id_str}"

    sprint_dicts = [_sprint_to_dict(s) for s in sprints]

    await redis_client.setex(
        tickets_key,
        _CACHE_TTL,
        json.dumps(all_ticket_dicts),
    )
    await redis_client.setex(
        sprint_key,
        _CACHE_TTL,
        json.dumps(sprint_dicts),
    )

    logger.info(
        "Pre-populated Redis cache keys '%s' and '%s' (TTL %ds).",
        tickets_key,
        sprint_key,
        _CACHE_TTL,
    )

    return program_uuid
