"""
Agent Loop Runner — Stage 4.

Responsibilities per cycle:
1. Generate a UUID4 run_id.
2. Read and increment the cycle_counter from operational_memory (PostgreSQL).
3. Load current sprint/ticket state from PostgreSQL → SimSprint objects.
4. Apply one round of simulation mutations (mutate_state).
5. Persist mutated ticket state back to PostgreSQL.
6. Publish mutation events to the Redis Stream.
7. Build the initial PipelineState and invoke the LangGraph pipeline.
8. Return a summary dict (run_id, cycle_number, ticket_count, etc.).

start_agent_loop() runs run_cycle() every 30 seconds indefinitely.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import redis.asyncio as aioredis
from sqlalchemy import select, update

from agents.graph import compiled_graph
from agents.state import PipelineState
from core.program_context import ProgramContext
from db.models import OperationalMemory, Sprint, Ticket
from db.session import AsyncSessionLocal
from simulation.engine import mutate_state
from simulation.event_publisher import publish_events
from simulation.models import SimSprint, SimTicket

logger = logging.getLogger(__name__)

_LOOP_INTERVAL_SECONDS = int(os.getenv("AGENT_LOOP_INTERVAL_SECONDS", "30"))


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _get_and_increment_cycle(
    program_uuid: uuid.UUID,
    domain: str,
) -> int:
    """
    Read cycle_counter from operational_memory and increment atomically.
    Creates the row on first call (returns 1).
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(OperationalMemory).where(
                OperationalMemory.program_id == program_uuid,
                OperationalMemory.key == "cycle_counter",
            )
        )
        row: Optional[OperationalMemory] = result.scalar_one_or_none()

        if row is None:
            new_count = 1
            session.add(
                OperationalMemory(
                    program_id=program_uuid,
                    domain=domain,
                    key="cycle_counter",
                    value={"count": new_count},
                )
            )
        else:
            new_count = row.value["count"] + 1
            # JSONB mutation requires explicit reassignment for SQLAlchemy
            # to detect the change
            row.value = {"count": new_count}

        await session.commit()
        return new_count


async def _load_sim_sprints(program_uuid: uuid.UUID) -> List[SimSprint]:
    """
    Load current sprint and ticket state from PostgreSQL and convert to
    SimSprint / SimTicket objects for the mutation engine.
    """
    async with AsyncSessionLocal() as session:
        sprints_result = await session.execute(
            select(Sprint).where(Sprint.program_id == program_uuid)
        )
        sprints = sprints_result.scalars().all()

        tickets_result = await session.execute(
            select(Ticket).where(Ticket.program_id == program_uuid)
        )
        db_tickets = tickets_result.scalars().all()

    tickets_by_sprint: dict[str, list[Ticket]] = {}
    for t in db_tickets:
        tickets_by_sprint.setdefault(t.sprint_id or "", []).append(t)

    sim_sprints: List[SimSprint] = []
    for sprint in sprints:
        sim_tickets: List[SimTicket] = []
        for t in tickets_by_sprint.get(sprint.id, []):
            # Strip timezone info so SimTicket stays consistently naive-UTC.
            # Telemetry Agent normalizes on read.
            def _naive(dt: Optional[datetime]) -> Optional[datetime]:
                if dt is None:
                    return None
                return dt.replace(tzinfo=None) if dt.tzinfo else dt

            sim_tickets.append(
                SimTicket(
                    id=t.id,
                    title=t.title,
                    status=t.status,
                    priority=t.priority,
                    assignee=t.assignee or "",
                    team=t.team or "",
                    sprint_id=t.sprint_id or "",
                    story_points=t.story_points or 0,
                    points_completed=t.points_completed or 0,
                    is_on_critical_path=t.is_on_critical_path or False,
                    blocker_ids=t.blocker_ids or [],
                    stale_since=_naive(t.stale_since),
                    scope_changed=t.scope_changed or False,
                    milestone_target=t.milestone_target,
                    risk_flag=t.risk_flag,
                    risk_severity=t.risk_severity,
                    risk_reason=t.risk_reason,
                    updated_at=_naive(t.updated_at) or datetime.utcnow(),
                    created_at=_naive(t.created_at) or datetime.utcnow(),
                )
            )

        def _naive_dt(dt: Optional[datetime]) -> datetime:
            if dt is None:
                return datetime.utcnow()
            return dt.replace(tzinfo=None) if dt.tzinfo else dt

        sim_sprints.append(
            SimSprint(
                id=sprint.id,
                name=sprint.name,
                start_date=_naive_dt(sprint.start_date),
                end_date=_naive_dt(sprint.end_date),
                tickets=sim_tickets,
            )
        )

    return sim_sprints


async def _persist_mutated_tickets(
    program_uuid: uuid.UUID,
    mutated_sprints: List[SimSprint],
) -> None:
    """
    Write mutated ticket fields back to PostgreSQL.
    Only the fields the simulation engine can change are updated.
    """
    async with AsyncSessionLocal() as session:
        for sprint in mutated_sprints:
            for t in sprint.tickets:
                await session.execute(
                    update(Ticket)
                    .where(
                        Ticket.id == t.id,
                        Ticket.program_id == program_uuid,
                    )
                    .values(
                        status=t.status,
                        points_completed=t.points_completed,
                        blocker_ids=t.blocker_ids,
                        stale_since=t.stale_since,
                        scope_changed=t.scope_changed,
                        story_points=t.story_points,
                        updated_at=t.updated_at,
                    )
                )
        await session.commit()


# ── Public API ────────────────────────────────────────────────────────────────

async def run_cycle(
    program_id: str,
    context: ProgramContext,
) -> dict:
    """
    Execute one full agent pipeline cycle.

    Returns a summary dict with keys:
        run_id, cycle_number, started_at, completed_at,
        ticket_count, sprint_summaries, tickets,
        decision_count, error_count
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    program_uuid = uuid.UUID(program_id)

    # ── 1. Increment cycle counter ────────────────────────────────────────
    cycle_number = await _get_and_increment_cycle(program_uuid, context.domain)

    # ── 2. Load current sim state + mutate ────────────────────────────────
    sim_sprints = await _load_sim_sprints(program_uuid)
    mutated_sprints, events = mutate_state(sim_sprints, context)

    # ── 3. Persist mutation back to PostgreSQL ────────────────────────────
    await _persist_mutated_tickets(program_uuid, mutated_sprints)

    # ── 4. Publish events to Redis Stream ─────────────────────────────────
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis_client = aioredis.from_url(redis_url)
    try:
        await publish_events(redis_client, program_id, events)
    finally:
        await redis_client.aclose()

    # ── 5. Build initial pipeline state ───────────────────────────────────
    initial_state: PipelineState = {
        "run_id": run_id,
        "cycle_number": cycle_number,
        "started_at": started_at,
        "program_id": program_id,
        "program_context": context.model_dump(mode="json"),
        "tickets": [],
        "sprint_summaries": [],
        "risk_flags": [],
        "sprint_health": [],
        "mitigations": [],
        "executive_outputs": {},
        "agent_decisions": [],
        "errors": [],
    }

    # ── 6. Run LangGraph pipeline ─────────────────────────────────────────
    final_state: PipelineState = await compiled_graph.ainvoke(initial_state)

    completed_at = datetime.now(timezone.utc).isoformat()

    logger.info(
        "Cycle complete: run_id=%s cycle=%d tickets=%d decisions=%d errors=%d",
        run_id[:8],
        cycle_number,
        len(final_state.get("tickets", [])),
        len(final_state.get("agent_decisions", [])),
        len(final_state.get("errors", [])),
    )

    return {
        "run_id": run_id,
        "cycle_number": cycle_number,
        "started_at": started_at,
        "completed_at": completed_at,
        "ticket_count": len(final_state.get("tickets", [])),
        "sprint_summaries": final_state.get("sprint_summaries", []),
        "tickets": final_state.get("tickets", []),
        "decision_count": len(final_state.get("agent_decisions", [])),
        "error_count": len(final_state.get("errors", [])),
    }


async def start_agent_loop(
    program_id: str,
    context: ProgramContext,
) -> None:
    """
    Run the agent pipeline every AGENT_LOOP_INTERVAL_SECONDS indefinitely.

    - Logs cycle start/completion with run_id.
    - On non-cancellation exceptions: logs the error and continues.
    - On CancelledError: propagates (allows clean shutdown).
    """
    logger.info(
        "Agent loop starting — program_id=%s interval=%ds",
        program_id,
        _LOOP_INTERVAL_SECONDS,
    )

    while True:
        try:
            logger.info("Agent loop: starting cycle (program_id=%s)", program_id)
            result = await run_cycle(program_id, context)
            logger.info(
                "Agent loop: cycle %d complete — run_id=%s tickets=%d",
                result["cycle_number"],
                result["run_id"][:8],
                result["ticket_count"],
            )
        except asyncio.CancelledError:
            logger.info("Agent loop: cancelled cleanly.")
            raise
        except Exception:
            logger.exception(
                "Agent loop: unhandled exception in cycle — continuing."
            )

        await asyncio.sleep(_LOOP_INTERVAL_SECONDS)
