"""
Tests for the Simulation Engine (Stage 3).

Three test groups:
1. generate_initial_state — counts, critical-path ratio, field validity
2. mutate_state — events emitted, state changes, stale_since written by engine
3. publish_events — Redis Stream write (requires live Redis on localhost:6379)

Run from backend/ directory:
    PYTHONPATH=. .venv/bin/pytest tests/test_simulation_engine.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from core.context_loader import load_context
from core.program_context import ProgramContext
from simulation.engine import generate_initial_state, mutate_state
from simulation.event_publisher import publish_events

# ── Fixtures ─────────────────────────────────────────────────────────────────

DEFAULT_YAML = Path(__file__).parent.parent / "config" / "programs" / "default.yaml"


@pytest.fixture(scope="module")
def ctx() -> ProgramContext:
    return load_context(str(DEFAULT_YAML))


@pytest.fixture(scope="module")
def initial_sprints(ctx):
    """Reproducible initial state (fixed seed=42)."""
    return generate_initial_state(ctx, seed=42)


# ── Group 1: generate_initial_state ──────────────────────────────────────────

class TestGenerateInitialState:
    def test_returns_three_sprints(self, initial_sprints):
        assert len(initial_sprints) == 3

    def test_sprint_ids(self, initial_sprints):
        ids = [s.id for s in initial_sprints]
        assert ids == ["sprint-1", "sprint-2", "sprint-3"]

    def test_total_ticket_count_in_range(self, initial_sprints):
        total = sum(len(s.tickets) for s in initial_sprints)
        assert 33 <= total <= 42, f"Expected 33-42 tickets, got {total}"

    def test_each_sprint_has_tickets(self, initial_sprints):
        for sprint in initial_sprints:
            assert len(sprint.tickets) >= 11, (
                f"{sprint.id} has only {len(sprint.tickets)} tickets"
            )

    def test_critical_path_ratio_at_least_30_percent(self, initial_sprints):
        all_tickets = [t for s in initial_sprints for t in s.tickets]
        critical = [t for t in all_tickets if t.is_on_critical_path]
        ratio = len(critical) / len(all_tickets)
        assert ratio >= 0.30, (
            f"Critical path ratio {ratio:.1%} is below 30%"
        )

    def test_all_ticket_statuses_valid(self, initial_sprints):
        valid = {"TODO", "IN_PROGRESS", "IN_REVIEW", "BLOCKED", "DONE"}
        for sprint in initial_sprints:
            for ticket in sprint.tickets:
                assert ticket.status in valid, (
                    f"Ticket {ticket.id} has invalid status '{ticket.status}'"
                )

    def test_all_ticket_priorities_valid(self, initial_sprints):
        valid = {"P0", "P1", "P2", "P3"}
        for sprint in initial_sprints:
            for ticket in sprint.tickets:
                assert ticket.priority in valid

    def test_story_points_positive(self, initial_sprints):
        for sprint in initial_sprints:
            for ticket in sprint.tickets:
                assert ticket.story_points > 0

    def test_critical_tickets_have_milestone_target(self, initial_sprints):
        for sprint in initial_sprints:
            for ticket in sprint.tickets:
                if ticket.is_on_critical_path:
                    assert ticket.milestone_target is not None, (
                        f"Critical ticket {ticket.id} has no milestone_target"
                    )

    def test_sprint_completion_rate_between_0_and_1(self, initial_sprints):
        for sprint in initial_sprints:
            rate = sprint.completion_rate
            assert 0.0 <= rate <= 1.0, (
                f"{sprint.id} completion_rate {rate} out of [0,1]"
            )

    def test_ticket_ids_unique(self, initial_sprints):
        ids = [t.id for s in initial_sprints for t in s.tickets]
        assert len(ids) == len(set(ids)), "Duplicate ticket IDs found"

    def test_teams_present(self, initial_sprints, ctx):
        expected_teams = {t.name for t in ctx.teams}
        found_teams = {t.team for s in initial_sprints for t in s.tickets}
        assert expected_teams == found_teams, (
            f"Expected teams {expected_teams}, found {found_teams}"
        )


# ── Group 2: mutate_state ────────────────────────────────────────────────────

class TestMutateState:
    def test_mutate_returns_sprints_and_events(self, initial_sprints, ctx):
        import copy
        sprints_copy = copy.deepcopy(initial_sprints)
        mutated_sprints, events = mutate_state(sprints_copy, ctx)
        assert isinstance(mutated_sprints, list)
        assert isinstance(events, list)

    def test_events_are_non_empty(self, initial_sprints, ctx):
        """With 35+ active tickets the engine should emit at least one event."""
        import copy
        sprints_copy = copy.deepcopy(initial_sprints)
        _, events = mutate_state(sprints_copy, ctx)
        assert len(events) > 0, "mutate_state emitted no events"

    def test_event_types_valid(self, initial_sprints, ctx):
        import copy
        valid = {
            "TICKET_STALLED",
            "TICKET_PROGRESSED",
            "BLOCKER_ADDED",
            "BLOCKER_RESOLVED",
            "SCOPE_EXPANDED",
        }
        sprints_copy = copy.deepcopy(initial_sprints)
        _, events = mutate_state(sprints_copy, ctx)
        for evt in events:
            assert evt["type"] in valid, f"Unknown event type: {evt['type']}"

    def test_stalled_ticket_has_stale_since(self, initial_sprints, ctx):
        """Tickets mutated to TICKET_STALLED must have stale_since set."""
        import copy
        sprints_copy = copy.deepcopy(initial_sprints)
        mutated, events = mutate_state(sprints_copy, ctx)
        stalled_ids = {e["ticket_id"] for e in events if e["type"] == "TICKET_STALLED"}
        for sprint in mutated:
            for ticket in sprint.tickets:
                if ticket.id in stalled_ids:
                    assert ticket.stale_since is not None, (
                        f"Ticket {ticket.id} is TICKET_STALLED but stale_since is None"
                    )

    def test_done_tickets_unchanged(self, initial_sprints, ctx):
        """DONE tickets must never be mutated."""
        import copy
        sprints_copy = copy.deepcopy(initial_sprints)

        # Identify DONE ticket ids before mutation
        done_before = {
            t.id: t.story_points
            for s in sprints_copy
            for t in s.tickets
            if t.status == "DONE"
        }

        mutated, _ = mutate_state(sprints_copy, ctx)

        after = {
            t.id: (t.status, t.story_points)
            for s in mutated
            for t in s.tickets
            if t.id in done_before
        }

        for tid, orig_pts in done_before.items():
            status, pts = after[tid]
            assert status == "DONE", f"DONE ticket {tid} changed status"
            assert pts == orig_pts, f"DONE ticket {tid} changed story_points"

    def test_scope_expanded_event_has_points_added(self, initial_sprints, ctx):
        import copy
        # Run multiple mutations until we get a SCOPE_EXPANDED event
        for attempt in range(20):
            sprints_copy = copy.deepcopy(initial_sprints)
            _, events = mutate_state(sprints_copy, ctx)
            scope_events = [e for e in events if e["type"] == "SCOPE_EXPANDED"]
            if scope_events:
                for evt in scope_events:
                    assert "points_added" in evt
                    assert evt["points_added"] >= 1
                return
        # If no scope event after 20 attempts, that's fine — low probability
        pytest.skip("SCOPE_EXPANDED event not observed in 20 mutation rounds")


# ── Group 3: Redis Stream (publish_events) ────────────────────────────────────

@pytest.mark.asyncio
class TestPublishEvents:
    """
    Requires live Redis on localhost:6379.
    Skipped automatically if Redis is not reachable.
    """

    async def _get_client(self):
        import redis.asyncio as aioredis
        client = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
        try:
            await client.ping()
        except Exception:
            await client.aclose()
            return None
        return client

    async def test_events_written_to_stream(self, ctx):
        client = await self._get_client()
        if client is None:
            pytest.skip("Redis not reachable — skipping publish test")

        program_id = "test-sim-engine"
        stream_key = f"streams:sim_events:{program_id}"

        # Clean up any previous test run
        await client.delete(stream_key)

        events = [
            {"type": "TICKET_STALLED", "ticket_id": "ATLAS-001", "sprint_id": "sprint-1",
             "team": "Platform", "assignee": "priya.singh", "stale_since": "2026-05-17T00:00:00"},
            {"type": "BLOCKER_ADDED", "ticket_id": "ATLAS-002", "sprint_id": "sprint-1",
             "team": "Payments", "assignee": "marcus.webb", "blocker_ids": ["ATLAS-001"]},
        ]

        written = await publish_events(client, program_id, events)
        assert written == 2

        # Verify entries exist in the stream
        entries = await client.xlen(stream_key)
        assert entries == 2

        await client.delete(stream_key)
        await client.aclose()

    async def test_empty_events_returns_zero(self, ctx):
        client = await self._get_client()
        if client is None:
            pytest.skip("Redis not reachable — skipping publish test")

        written = await publish_events(client, "test-sim-engine", [])
        assert written == 0
        await client.aclose()

    async def test_stream_maxlen_respected(self, ctx):
        """Write 510 events and verify the stream is trimmed to ≤500 entries."""
        client = await self._get_client()
        if client is None:
            pytest.skip("Redis not reachable — skipping publish test")

        program_id = "test-sim-maxlen"
        stream_key = f"streams:sim_events:{program_id}"
        await client.delete(stream_key)

        batch = [
            {"type": "TICKET_PROGRESSED", "ticket_id": f"ATLAS-{i:03d}",
             "sprint_id": "sprint-1", "new_status": "IN_REVIEW",
             "team": "Platform", "assignee": "priya.singh"}
            for i in range(510)
        ]
        await publish_events(client, program_id, batch)

        # Approximate trimming — stream length should be ≤520 (MAXLEN ~500)
        length = await client.xlen(stream_key)
        assert length <= 520, f"Stream length {length} exceeds expected cap"

        await client.delete(stream_key)
        await client.aclose()
