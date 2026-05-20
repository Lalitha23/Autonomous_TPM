"""
Tests for the Telemetry Agent and agent loop runner — Stage 4.

Three tests:
1. test_telemetry_populates_tickets   — run_cycle produces tickets + sprint summaries
2. test_telemetry_logs_decision       — agent_decisions table row written with reasoning
3. test_agent_loop_runs_multiple_cycles — cycle_number advances over 75 s (~3 cycles)

Requires live PostgreSQL and Redis. Skips if either is unreachable.

Run all tests (including the 75-second loop test):
    PYTHONPATH=. .venv/bin/pytest tests/test_telemetry_agent.py -v

Run without the slow loop test:
    PYTHONPATH=. .venv/bin/pytest tests/test_telemetry_agent.py -v -m "not slow"
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select

from agents.runner import run_cycle, start_agent_loop
from core.context_loader import load_context
from db.models import AgentDecision
from db.session import AsyncSessionLocal
from simulation.seeder import seed_program

DEFAULT_YAML = Path(__file__).parent.parent / "config" / "programs" / "default.yaml"


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ctx():
    return load_context(str(DEFAULT_YAML))


async def _check_redis() -> bool:
    """Return True if Redis is reachable."""
    try:
        r = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0")
        )
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


async def _check_db() -> bool:
    """Return True if PostgreSQL is reachable."""
    from sqlalchemy import text
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture
async def program_id(ctx):
    """
    Seed the program (idempotent) and return its UUID string.
    Skips the test if PostgreSQL or Redis is not reachable.
    """
    if not await _check_db():
        pytest.skip("PostgreSQL not reachable — skipping telemetry agent tests")
    if not await _check_redis():
        pytest.skip("Redis not reachable — skipping telemetry agent tests")

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url)
    try:
        async with AsyncSessionLocal() as db:
            program_uuid = await seed_program(db, r, ctx)
    finally:
        await r.aclose()

    return str(program_uuid)


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_telemetry_populates_tickets(program_id, ctx):
    """
    run_cycle must produce a non-empty tickets list and exactly 3 sprint
    summaries, each with a pct_complete field.
    """
    result = await run_cycle(program_id, ctx)

    assert len(result["tickets"]) > 0, (
        "Expected non-empty tickets list from telemetry agent"
    )
    assert len(result["sprint_summaries"]) == 3, (
        f"Expected 3 sprint summaries, got {len(result['sprint_summaries'])}"
    )
    for summary in result["sprint_summaries"]:
        assert "pct_complete" in summary, (
            f"sprint_summary missing 'pct_complete': {summary}"
        )
        assert isinstance(summary["pct_complete"], (int, float))


async def test_telemetry_logs_decision(program_id, ctx):
    """
    run_cycle must write at least one agent_decisions row with
    agent_name='telemetry' and a non-empty reasoning field.
    """
    await run_cycle(program_id, ctx)

    program_uuid = uuid.UUID(program_id)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AgentDecision).where(
                AgentDecision.program_id == program_uuid,
                AgentDecision.agent_name == "telemetry",
            )
        )
        decisions = result.scalars().all()

    assert len(decisions) > 0, (
        "No agent_decisions rows found with agent_name='telemetry'"
    )
    for d in decisions:
        assert d.reasoning, (
            f"Decision row {d.id} has empty reasoning field"
        )


@pytest.mark.slow
async def test_agent_loop_runs_multiple_cycles(program_id, ctx):
    """
    Start the agent loop and let it run for 75 seconds (~3 cycles at 30s
    intervals). Then confirm that cycle_number advances across the decisions
    written during that window.

    This test is marked @pytest.mark.slow and takes approximately 75 seconds.
    Skip with: pytest -m "not slow"
    """
    start_time = datetime.now(timezone.utc)
    program_uuid = uuid.UUID(program_id)

    loop_task = asyncio.create_task(
        start_agent_loop(program_id, ctx),
        name="test_agent_loop",
    )

    # Wait for ~2.5 cycles (first fires immediately, then at 30s, 60s)
    await asyncio.sleep(75)

    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    # Query only decisions created during this test's window
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AgentDecision).where(
                AgentDecision.program_id == program_uuid,
                AgentDecision.agent_name == "telemetry",
                AgentDecision.created_at >= start_time,
            ).order_by(AgentDecision.created_at)
        )
        decisions = result.scalars().all()

    cycle_numbers = [d.cycle_number for d in decisions]
    assert len(cycle_numbers) >= 2, (
        f"Expected >= 2 telemetry decisions in 75s window, got {len(cycle_numbers)}"
    )
    assert len(set(cycle_numbers)) >= 2, (
        f"Expected cycle_number to advance, got constant values: {cycle_numbers}"
    )
