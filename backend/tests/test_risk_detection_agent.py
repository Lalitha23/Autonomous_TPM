"""
Tests for Risk Detection Agent and Dependency Analysis utilities — Stage 5.

Five tests:
1. test_stale_ticket_detection            — pure function
2. test_critical_path_severity_multiplier — pure function
3. test_blocked_ticket_detection          — pure function
4. test_sprint_health_badge_escalate      — live DB required
5. test_dependency_chain_traced           — pure function

Run all tests (excluding the slow Stage 4 loop test):
    PYTHONPATH=. .venv/bin/pytest tests/test_risk_detection_agent.py -v
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select, update

from agents.dependency_analysis_agent import (
    build_dependency_graph,
    trace_chain,
)
from agents.risk_detection_agent import (
    detect_blocked_tickets,
    detect_stale_tickets,
)
from agents.runner import run_cycle
from core.context_loader import load_context
from db.models import Sprint, Ticket
from db.session import AsyncSessionLocal
from simulation.seeder import seed_program

DEFAULT_YAML = Path(__file__).parent.parent / "config" / "programs" / "default.yaml"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ctx():
    return load_context(str(DEFAULT_YAML))


def _make_ticket(**overrides) -> dict:
    """Build a minimal ticket dict with sensible defaults."""
    base = {
        "id": f"TEST-{uuid.uuid4().hex[:6].upper()}",
        "title": "Test ticket",
        "status": "IN_PROGRESS",
        "priority": "P2",
        "assignee": "dev.one",
        "team": "Platform",
        "sprint_id": "sprint-1",
        "story_points": 3,
        "points_completed": 0,
        "is_on_critical_path": False,
        "blocker_ids": [],
        "stale_since": None,
        "stale_candidate": False,
        "scope_changed": False,
        "milestone_target": None,
        "risk_flag": None,
        "risk_severity": None,
        "risk_reason": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


async def _check_db() -> bool:
    from sqlalchemy import text
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _check_redis() -> bool:
    try:
        r = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


@pytest.fixture
async def program_id(ctx):
    """Idempotent program seed; skips if infrastructure not reachable."""
    if not await _check_db():
        pytest.skip("PostgreSQL not reachable")
    if not await _check_redis():
        pytest.skip("Redis not reachable")

    r = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    try:
        async with AsyncSessionLocal() as db:
            program_uuid = await seed_program(db, r, ctx)
    finally:
        await r.aclose()
    return str(program_uuid)


# ── Test 1: detect_stale_tickets — pure function ──────────────────────────────

def test_stale_ticket_detection(ctx):
    """
    A ticket stale for 5 days with threshold=3 must produce one STALE flag
    with severity at least MEDIUM (5 >= 3*2=6? No — 5 < 6 so base is LOW;
    with P2 non-critical stays LOW; but with stale_since far enough it bumps).

    Use 7 days (>= threshold*2=6) to guarantee MEDIUM base.
    """
    now = datetime.now(timezone.utc)
    stale_time = (now - timedelta(days=7)).isoformat()

    ticket = _make_ticket(
        stale_since=stale_time,
        priority="P2",
        is_on_critical_path=False,
    )

    flags = detect_stale_tickets([ticket], ctx)

    assert len(flags) == 1, f"Expected 1 STALE flag, got {len(flags)}"
    assert flags[0]["flag"] == "STALE"
    assert flags[0]["ticket_id"] == ticket["id"]

    sev_order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    assert sev_order.index(flags[0]["severity"]) >= sev_order.index("MEDIUM"), (
        f"Expected severity >= MEDIUM for 7-day stale ticket, got {flags[0]['severity']}"
    )


# ── Test 2: critical path severity multiplier ──────────────────────────────────

def test_critical_path_severity_multiplier(ctx):
    """
    Same stale ticket but is_on_critical_path=True must elevate severity
    to HIGH or CRITICAL (MEDIUM base → bump → HIGH, P2 so no further bump).
    """
    now = datetime.now(timezone.utc)
    stale_time = (now - timedelta(days=7)).isoformat()  # MEDIUM base

    ticket = _make_ticket(
        stale_since=stale_time,
        priority="P2",
        is_on_critical_path=True,
    )

    flags = detect_stale_tickets([ticket], ctx)

    assert len(flags) == 1
    sev_order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    assert sev_order.index(flags[0]["severity"]) >= sev_order.index("HIGH"), (
        f"Expected severity >= HIGH for critical-path stale ticket, "
        f"got {flags[0]['severity']}"
    )


# ── Test 3: detect_blocked_tickets — pure function ────────────────────────────

def test_blocked_ticket_detection(ctx):
    """
    A ticket whose blocker_ids points to an IN_PROGRESS ticket must produce
    one BLOCKED flag.
    """
    blocker = _make_ticket(
        id="BLOCKER-001",
        status="IN_PROGRESS",
    )
    blocked = _make_ticket(
        id="BLOCKED-001",
        status="BLOCKED",
        blocker_ids=["BLOCKER-001"],
    )

    flags = detect_blocked_tickets([blocker, blocked], ctx)

    assert len(flags) == 1, f"Expected 1 BLOCKED flag, got {len(flags)}"
    assert flags[0]["flag"] == "BLOCKED"
    assert flags[0]["ticket_id"] == "BLOCKED-001"


# ── Test 4: sprint health badge ESCALATE — live DB ────────────────────────────

async def test_sprint_health_badge_escalate(program_id, ctx):
    """
    Manufacture a ticket guaranteed to produce CRITICAL severity
    (stale 10 days, P0 priority, on critical path → MEDIUM→HIGH→CRITICAL),
    run one cycle, and assert the sprint has ESCALATE badge in PostgreSQL.
    """
    program_uuid = uuid.UUID(program_id)
    now = datetime.now(timezone.utc)

    # Find any ticket for this program — then force it to non-DONE.
    # (After many Stage 4 mutation cycles all tickets can reach DONE.)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Ticket).where(
                Ticket.program_id == program_uuid,
            ).limit(1)
        )
        ticket_row = result.scalar_one_or_none()

    assert ticket_row is not None, (
        "No tickets found for this program. Is the program seeded?"
    )
    target_ticket_id = ticket_row.id
    target_sprint_id = ticket_row.sprint_id

    # Manufacture CRITICAL conditions:
    #   stale_since = 10 days → base MEDIUM (>= threshold*2=6)
    #   is_on_critical_path=True → MEDIUM → HIGH
    #   priority=P0 → HIGH → CRITICAL
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Ticket)
            .where(Ticket.id == target_ticket_id)
            .values(
                stale_since=now - timedelta(days=10),
                is_on_critical_path=True,
                priority="P0",
                status="IN_PROGRESS",
            )
        )
        await db.commit()

    # Run one cycle — Risk Detection will see the CRITICAL stale ticket
    await run_cycle(program_id, ctx)

    # Assert the target sprint has ESCALATE badge in PostgreSQL
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Sprint).where(
                Sprint.program_id == program_uuid,
                Sprint.id == target_sprint_id,
            )
        )
        sprint = result.scalar_one_or_none()

    assert sprint is not None, f"{target_sprint_id} row not found"
    assert sprint.health_badge == "ESCALATE", (
        f"Expected ESCALATE badge for {target_sprint_id} (has CRITICAL flag), "
        f"got {sprint.health_badge!r}"
    )
    assert sprint.last_run_id is not None, "last_run_id should be set after cycle"
    assert sprint.worst_severity == "CRITICAL", (
        f"Expected worst_severity=CRITICAL, got {sprint.worst_severity!r}"
    )


# ── Test 5: dependency chain traced — pure function ───────────────────────────

def test_dependency_chain_traced(ctx):
    """
    Chain: A blocks B blocks C.
    trace_chain("C", graph) must return a chain containing A and B
    with chain_depth == 2.
    """
    ticket_a = _make_ticket(id="A", status="IN_PROGRESS", blocker_ids=[])
    ticket_b = _make_ticket(id="B", status="IN_PROGRESS", blocker_ids=["A"])
    ticket_c = _make_ticket(id="C", status="BLOCKED",     blocker_ids=["B"])

    tickets = [ticket_a, ticket_b, ticket_c]
    graph = build_dependency_graph(tickets)

    # Verify graph structure
    assert graph["C"] == ["B"], f"C should be blocked by B, got {graph['C']}"
    assert graph["B"] == ["A"], f"B should be blocked by A, got {graph['B']}"

    chain = trace_chain("C", graph)
    chain_depth = len(chain)

    assert "A" in chain, f"Expected A in dependency chain, got {chain}"
    assert "B" in chain, f"Expected B in dependency chain, got {chain}"
    assert chain_depth == 2, (
        f"Expected chain_depth=2 (A→B→C has 2 hops), got {chain_depth}"
    )
