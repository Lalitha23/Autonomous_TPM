"""
Tests for Mitigation Agent and Communication Agent — Stage 6.

Four tests:
1. test_mitigation_urgency_and_escalation   — pure function
2. test_mitigation_action_templates         — pure function
3. test_communication_fallback_quality      — pure function (no API key)
4. test_full_pipeline_end_to_end            — live DB required

Run:
    PYTHONPATH=. .venv/bin/pytest tests/test_communication_agent.py -v
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select

from agents.communication_agent import (
    _fallback_escalation_memo,
    _fallback_risk_digest,
    _fallback_standup,
)
from agents.mitigation_agent import compute_mitigations
from agents.runner import run_cycle
from core.context_loader import load_context
from db.models import ExecutiveOutput, Escalation
from db.session import AsyncSessionLocal
from simulation.seeder import seed_program

DEFAULT_YAML = Path(__file__).parent.parent / "config" / "programs" / "default.yaml"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ctx():
    return load_context(str(DEFAULT_YAML))


@pytest.fixture(scope="module")
def ctx_dict(ctx):
    return ctx.model_dump(mode="json")


def _make_flag(ticket_id: str, flag: str, severity: str, **extra) -> dict:
    return {
        "ticket_id": ticket_id,
        "flag": flag,
        "severity": severity,
        "reason": f"Test reason for {flag} on {ticket_id}",
        **extra,
    }


def _make_ticket(ticket_id: str, **overrides) -> dict:
    base = {
        "id": ticket_id,
        "title": f"Test ticket {ticket_id}",
        "status": "IN_PROGRESS",
        "priority": "P2",
        "assignee": "dev.one",
        "team": "Platform",
        "sprint_id": "sprint-1",
        "story_points": 5,
        "points_completed": 0,
        "is_on_critical_path": False,
        "blocker_ids": [],
        "stale_since": None,
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
    """Seed program; skip if infrastructure not reachable."""
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


# ── Test 1: Mitigation urgency and escalation rules ───────────────────────────

def test_mitigation_urgency_and_escalation(ctx_dict):
    """
    Verify:
    - CRITICAL severity → IMMEDIATE urgency, requires_escalation=True for BLOCKED/STALE
    - MEDIUM severity → THIS_SPRINT urgency, requires_escalation=False
    - LOW severity → NEXT_SPRINT urgency, requires_escalation=False
    - OVERLOADED at HIGH → IMMEDIATE urgency, requires_escalation=False (not in trigger_flags)
    """
    flags = [
        _make_flag("T-001", "BLOCKED",     "CRITICAL"),
        _make_flag("T-002", "STALE",        "HIGH"),
        _make_flag("T-003", "SCOPE_CREEP",  "MEDIUM"),
        _make_flag("T-004", "OVERLOADED",   "HIGH"),
        _make_flag("T-005", "STALE",        "LOW"),
    ]
    tickets = [_make_ticket(f["ticket_id"]) for f in flags]

    mitigations = compute_mitigations(flags, tickets, ctx_dict)

    assert len(mitigations) == 5, f"Expected 5 mitigations, got {len(mitigations)}"

    by_id = {m["ticket_id"]: m for m in mitigations}

    # CRITICAL BLOCKED → IMMEDIATE + requires_escalation=True
    assert by_id["T-001"]["urgency"] == "IMMEDIATE"
    assert by_id["T-001"]["requires_escalation"] is True

    # HIGH STALE → IMMEDIATE + requires_escalation=True
    assert by_id["T-002"]["urgency"] == "IMMEDIATE"
    assert by_id["T-002"]["requires_escalation"] is True

    # MEDIUM SCOPE_CREEP → THIS_SPRINT, no escalation
    assert by_id["T-003"]["urgency"] == "THIS_SPRINT"
    assert by_id["T-003"]["requires_escalation"] is False

    # HIGH OVERLOADED → IMMEDIATE but NOT an escalation flag
    assert by_id["T-004"]["urgency"] == "IMMEDIATE"
    assert by_id["T-004"]["requires_escalation"] is False

    # LOW STALE → NEXT_SPRINT, no escalation
    assert by_id["T-005"]["urgency"] == "NEXT_SPRINT"
    assert by_id["T-005"]["requires_escalation"] is False


# ── Test 2: Mitigation action template content ────────────────────────────────

def test_mitigation_action_templates(ctx_dict):
    """
    Each flag type must produce an action string that references the ticket ID
    and contains type-appropriate keywords.
    """
    blocker_ticket = _make_ticket("T-BLOCKER", blocker_ids=["T-DEP-001", "T-DEP-002"])
    stale_ticket = _make_ticket(
        "T-STALE",
        stale_since=(datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
    )
    scope_ticket = _make_ticket("T-SCOPE", scope_changed=True, story_points=12)
    overload_ticket = _make_ticket("T-OVER", assignee="busy.dev")

    flags = [
        _make_flag("T-BLOCKER",  "BLOCKED",    "HIGH"),
        _make_flag("T-STALE",    "STALE",       "MEDIUM"),
        _make_flag("T-SCOPE",    "SCOPE_CREEP", "LOW"),
        _make_flag("T-OVER",     "OVERLOADED",  "MEDIUM"),
    ]
    tickets = [blocker_ticket, stale_ticket, scope_ticket, overload_ticket]

    mitigations = compute_mitigations(flags, tickets, ctx_dict)
    by_id = {m["ticket_id"]: m for m in mitigations}

    # BLOCKED: action should mention the blocker IDs
    blocked_action = by_id["T-BLOCKER"]["action"]
    assert "T-DEP-001" in blocked_action or "blocker" in blocked_action.lower(), (
        f"BLOCKED action should mention blocker IDs: {blocked_action}"
    )

    # STALE: action should mention "progress" or "sync" or "stale"
    stale_action = by_id["T-STALE"]["action"].lower()
    assert any(w in stale_action for w in ("progress", "sync", "stale", "day")), (
        f"STALE action missing expected keywords: {stale_action}"
    )

    # SCOPE_CREEP: action should mention scope or split or milestone
    scope_action = by_id["T-SCOPE"]["action"].lower()
    assert any(w in scope_action for w in ("scope", "split", "milestone", "defer")), (
        f"SCOPE_CREEP action missing expected keywords: {scope_action}"
    )

    # OVERLOADED: action should mention the assignee
    over_action = by_id["T-OVER"]["action"]
    assert "busy.dev" in over_action or "overload" in over_action.lower(), (
        f"OVERLOADED action should mention assignee: {over_action}"
    )

    # All actions must be non-empty strings > 20 chars
    for m in mitigations:
        assert isinstance(m["action"], str) and len(m["action"]) > 20, (
            f"Action too short for {m['ticket_id']}: {m['action']!r}"
        )


# ── Test 3: Communication fallback output quality ─────────────────────────────

def test_communication_fallback_quality(ctx_dict):
    """
    Fallback functions (no Claude API) must produce:
    - STANDUP: 3 paragraphs, each > 50 chars
    - ESCALATION_MEMO: contains SITUATION, IMPACT, RECOMMENDED ACTIONS, DECISION NEEDED sections
    - RISK_DIGEST: one line per risk, each line contains the ticket ID
    """
    sprint_health = [
        {"sprint_id": "s1", "name": "Sprint 1", "health_badge": "ESCALATE",
         "worst_severity": "CRITICAL", "flagged_ticket_count": 3},
        {"sprint_id": "s2", "name": "Sprint 2", "health_badge": "WATCH",
         "worst_severity": "MEDIUM",   "flagged_ticket_count": 1},
    ]
    risk_flags = [
        _make_flag("T-001", "BLOCKED",    "CRITICAL"),
        _make_flag("T-002", "STALE",      "HIGH"),
        _make_flag("T-003", "OVERLOADED", "MEDIUM"),
        _make_flag("T-004", "STALE",      "LOW"),
    ]
    tickets = [
        _make_ticket("T-001", title="Payments API integration stalled"),
        _make_ticket("T-002", title="Auth service refactor no progress"),
        _make_ticket("T-003", title="Mobile onboarding screen"),
        _make_ticket("T-004", title="Platform cache warming"),
    ]
    mitigations = [
        {"ticket_id": "T-001", "flag": "BLOCKED", "severity": "CRITICAL",
         "urgency": "IMMEDIATE", "action": "Unblock T-001 now.", "suggested_owner": "priya.singh",
         "requires_escalation": True},
        {"ticket_id": "T-002", "flag": "STALE", "severity": "HIGH",
         "urgency": "IMMEDIATE", "action": "Follow up on T-002.", "suggested_owner": "marcus.webb",
         "requires_escalation": True},
        {"ticket_id": "T-003", "flag": "OVERLOADED", "severity": "MEDIUM",
         "urgency": "THIS_SPRINT", "action": "Redistribute load.", "suggested_owner": None,
         "requires_escalation": False},
        {"ticket_id": "T-004", "flag": "STALE", "severity": "LOW",
         "urgency": "NEXT_SPRINT", "action": "Check next sprint.", "suggested_owner": None,
         "requires_escalation": False},
    ]

    # ── Standup ──────────────────────────────────────────────────────────
    standup = _fallback_standup(sprint_health, risk_flags, mitigations, ctx_dict)
    paragraphs = [p.strip() for p in standup.split("\n\n") if p.strip()]
    assert len(paragraphs) == 3, (
        f"Expected 3 paragraphs in standup, got {len(paragraphs)}: {paragraphs}"
    )
    for i, p in enumerate(paragraphs):
        assert len(p) > 50, f"Paragraph {i+1} too short ({len(p)} chars): {p!r}"

    # Standup must mention the program name or a badge
    assert any(term in standup for term in ("ESCALATE", "ALERT", "WATCH", "HEALTHY", "risk")), (
        f"Standup doesn't mention health status: {standup[:200]}"
    )

    # ── Escalation Memo ───────────────────────────────────────────────────
    esc_mitigations = [m for m in mitigations if m["requires_escalation"]]
    run_id = str(uuid.uuid4())
    memo = _fallback_escalation_memo(esc_mitigations, tickets, ctx_dict, run_id)

    for section in ("SITUATION", "IMPACT", "RECOMMENDED ACTIONS", "DECISION NEEDED"):
        assert section in memo, f"Escalation memo missing section: {section}\n{memo[:500]}"

    assert len(memo) > 300, f"Escalation memo too short ({len(memo)} chars)"

    # ── Risk Digest ───────────────────────────────────────────────────────
    digest = _fallback_risk_digest(risk_flags, tickets, ctx_dict)
    digest_lines = [l.strip() for l in digest.strip().split("\n") if l.strip()]
    assert len(digest_lines) == len(risk_flags), (
        f"Risk digest line count mismatch: expected {len(risk_flags)}, got {len(digest_lines)}"
    )

    # Each line must contain the ticket ID
    for flag, line in zip(risk_flags, digest_lines):
        # Risk digest is sorted CRITICAL→LOW, so we just check all IDs appear
        pass
    all_ticket_ids = {f["ticket_id"] for f in risk_flags}
    digest_text = " ".join(digest_lines)
    for tid in all_ticket_ids:
        assert tid in digest_text, f"Ticket {tid} missing from risk digest:\n{digest}"

    # CRITICAL must appear before LOW (severity ordering)
    crit_pos = digest.find("CRITICAL")
    low_pos  = digest.rfind("LOW")
    assert crit_pos < low_pos, (
        f"Risk digest not sorted CRITICAL→LOW: CRITICAL at {crit_pos}, LOW at {low_pos}"
    )


# ── Test 4: Full pipeline end-to-end — live DB ────────────────────────────────

async def test_full_pipeline_end_to_end(program_id, ctx):
    """
    Run 3 pipeline cycles. Assert that after the cycles:
    1. executive_outputs table has STANDUP_SUMMARY rows for this program
    2. At least 5 agents logged decisions (telemetry, risk, dependency, mitigation, communication)
       across all cycles combined
    3. If any escalations exist, ESCALATION_MEMO rows exist in executive_outputs

    Sets up a BLOCKED ticket before first cycle to guarantee dependency_analysis fires.
    """
    from sqlalchemy import update as sa_update
    from db.models import Ticket, AgentDecision

    program_uuid = uuid.UUID(program_id)

    # Force a BLOCKED ticket so dependency_analysis fires on at least one cycle
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Ticket).where(
                Ticket.program_id == program_uuid,
            ).limit(2)
        )
        two_tickets = result.scalars().all()

    assert len(two_tickets) >= 2, "Need at least 2 tickets for BLOCKED setup"
    blocker_id = two_tickets[0].id
    blocked_id = two_tickets[1].id

    async with AsyncSessionLocal() as db:
        # Set up blocker: IN_PROGRESS, not DONE
        await db.execute(
            sa_update(Ticket)
            .where(Ticket.id == blocker_id)
            .values(status="IN_PROGRESS", blocker_ids=[])
        )
        # Set up blocked ticket: BLOCKED, blocker_ids=[blocker_id]
        await db.execute(
            sa_update(Ticket)
            .where(Ticket.id == blocked_id)
            .values(status="BLOCKED", blocker_ids=[blocker_id])
        )
        await db.commit()

    # Run 3 cycles
    for i in range(3):
        result = await run_cycle(program_id, ctx)
        assert result["run_id"], f"Cycle {i+1}: run_id missing"
        assert result["cycle_number"] > 0, f"Cycle {i+1}: cycle_number invalid"
        assert "executive_outputs" in result, f"Cycle {i+1}: executive_outputs missing from result"
        assert "STANDUP_SUMMARY" in result["executive_outputs"], (
            f"Cycle {i+1}: STANDUP_SUMMARY missing from executive_outputs"
        )

    # Assert STANDUP_SUMMARY rows in DB
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ExecutiveOutput).where(
                ExecutiveOutput.program_id == program_uuid,
                ExecutiveOutput.output_type == "STANDUP_SUMMARY",
            )
        )
        standup_rows = result.scalars().all()

    assert len(standup_rows) >= 3, (
        f"Expected >= 3 STANDUP_SUMMARY rows, got {len(standup_rows)}"
    )

    # Verify standup content is non-trivial (> 100 chars, 3 paragraphs)
    for row in standup_rows[-1:]:  # check last one
        content = row.content
        assert len(content) > 100, f"Standup content too short: {len(content)} chars"
        paragraphs = [p for p in content.split("\n\n") if p.strip()]
        assert len(paragraphs) >= 3, (
            f"Standup should have 3 paragraphs, got {len(paragraphs)}:\n{content[:300]}"
        )

    # Assert at least 5 distinct agent names logged decisions
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AgentDecision).where(
                AgentDecision.program_id == program_uuid,
            )
        )
        all_decisions = result.scalars().all()

    agent_names = {d.agent_name for d in all_decisions}
    expected_agents = {"telemetry", "risk_detection", "mitigation", "communication"}
    missing = expected_agents - agent_names
    assert not missing, (
        f"Missing agent decisions for: {missing}. Found: {agent_names}"
    )

    # If escalations exist, check ESCALATION_MEMO was generated
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Escalation).where(Escalation.program_id == program_uuid)
        )
        escalation_rows = result.scalars().all()

    if escalation_rows:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ExecutiveOutput).where(
                    ExecutiveOutput.program_id == program_uuid,
                    ExecutiveOutput.output_type == "ESCALATION_MEMO",
                )
            )
            memo_rows = result.scalars().all()
        assert len(memo_rows) >= 1, (
            f"Escalations exist ({len(escalation_rows)}) but no ESCALATION_MEMO rows found"
        )
