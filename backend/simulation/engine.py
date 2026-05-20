"""
Simulation Engine — generates and mutates a realistic TPM backlog state.

Design decisions:
- generate_initial_state() produces 3 sprints × ~12 tickets each (35-40 total).
  At least 30% of tickets are on the critical path (milestone-linked).
- mutate_state() applies stochastic mutations to simulate real sprint churn.
  Probabilities are driven by ProgramContext.simulation_weights so they can be
  tuned per program without code changes.
- The engine never writes to PostgreSQL or Redis; that is the seeder's job.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import List, Tuple

from core.program_context import ProgramContext
from simulation.models import SimSprint, SimTicket

# ── Seed data pools ──────────────────────────────────────────────────────────

_TITLES_BY_TEAM = {
    "Platform": [
        "Implement OAuth2 token refresh endpoint",
        "Harden rate-limiting on public API",
        "Migrate auth service to async handlers",
        "Add distributed tracing to API gateway",
        "Upgrade infrastructure Terraform modules",
        "Write load tests for /payments proxy route",
        "Audit API key rotation procedure",
        "Refactor middleware pipeline for latency",
        "Add gRPC health-check probe",
        "Document OpenAPI spec v3 update",
        "Fix connection pool exhaustion under load",
        "Set up blue-green deployment pipeline",
    ],
    "Payments": [
        "Implement idempotency keys for charge API",
        "Add webhook retry logic with back-off",
        "Migrate billing records to new schema",
        "Reconcile Stripe payout edge cases",
        "Write integration tests for refund flow",
        "Add 3DS2 challenge flow support",
        "Harden transaction rollback on timeout",
        "Build billing statement PDF generator",
        "Audit PCI-DSS scope for new microservice",
        "Implement proration logic for upgrades",
        "Fix race condition in concurrent charges",
        "Add currency normalization layer",
    ],
    "Mobile": [
        "Implement push notification opt-in flow",
        "Fix iOS deep-link routing regression",
        "Add biometric auth to Android app",
        "Optimise image loading in feed view",
        "Write UI tests for checkout funnel",
        "Migrate to new Firebase SDK",
        "Fix crash on Android 12 background limit",
        "Add offline-mode queue for actions",
        "Implement in-app update prompt",
        "Add accessibility labels to payment screen",
        "Fix memory leak in background sync",
        "Write snapshot tests for design system",
    ],
}

_PRIORITIES = ["P0", "P1", "P1", "P2", "P2", "P2", "P3"]  # weighted toward P2
_STATUSES_INITIAL = [
    "TODO", "TODO", "TODO",
    "IN_PROGRESS", "IN_PROGRESS", "IN_PROGRESS", "IN_PROGRESS",
    "IN_REVIEW", "IN_REVIEW",
    "BLOCKED",
    "DONE",
]

_LEADS = {
    "Platform": "priya.singh",
    "Payments": "marcus.webb",
    "Mobile": "sarah.chen",
}

_EXTRA_ASSIGNEES = {
    "Platform": ["alex.kim", "jordan.lee", "morgan.patel"],
    "Payments": ["taylor.wu", "casey.nguyen", "riley.okonkwo"],
    "Mobile": ["drew.santos", "quinn.baker", "avery.jones"],
}

# Milestone id → short label used in milestone_target
_MILESTONE_LABELS = {
    "m1": "API Freeze",
    "m2": "Integration Complete",
    "m3": "Beta Launch",
    "m4": "GA Launch",
}

# Which milestone id should critical-path tickets in each sprint target
_SPRINT_MILESTONE_MAP = {
    "sprint-1": "m1",
    "sprint-2": "m2",
    "sprint-3": "m3",
}


# ── Internal helpers ─────────────────────────────────────────────────────────

def _pick_assignee(team: str) -> str:
    pool = [_LEADS[team]] + _EXTRA_ASSIGNEES[team]
    return random.choice(pool)


def _make_ticket(
    ticket_id: str,
    team: str,
    sprint_id: str,
    title: str,
    is_critical: bool,
    base_date: datetime,
) -> SimTicket:
    status = random.choice(_STATUSES_INITIAL)
    story_pts = random.choice([1, 2, 3, 5, 8])
    points_done = story_pts if status == "DONE" else (
        random.randint(1, max(1, story_pts - 1)) if status == "IN_REVIEW" else 0
    )
    # Tickets updated randomly between 1 and 8 days ago
    updated_at = base_date - timedelta(days=random.randint(1, 8))

    milestone_id = _SPRINT_MILESTONE_MAP.get(sprint_id)
    milestone_target = _MILESTONE_LABELS.get(milestone_id) if is_critical else None

    blocker_ids: list[str] = []
    if status == "BLOCKED":
        # Generate a plausible blocker ticket id from the same sprint
        blocker_num = int(ticket_id.split("-")[1]) - 1
        if blocker_num > 0:
            blocker_ids = [f"ATLAS-{blocker_num:03d}"]

    return SimTicket(
        id=ticket_id,
        title=title,
        status=status,
        priority=random.choice(_PRIORITIES),
        assignee=_pick_assignee(team),
        team=team,
        sprint_id=sprint_id,
        story_points=story_pts,
        points_completed=points_done,
        is_on_critical_path=is_critical,
        blocker_ids=blocker_ids,
        milestone_target=milestone_target,
        updated_at=updated_at,
        created_at=updated_at - timedelta(days=random.randint(1, 5)),
    )


# ── Public API ───────────────────────────────────────────────────────────────

def generate_initial_state(
    ctx: ProgramContext,
    seed: int | None = None,
) -> List[SimSprint]:
    """
    Generate a fully-populated initial backlog for the given program.

    Returns 3 SimSprint objects, each containing 11-14 SimTickets.
    At least 30% of all tickets are on the critical path.

    Args:
        ctx:  Loaded ProgramContext (from default.yaml or equivalent).
        seed: Optional RNG seed for reproducible output in tests.

    Returns:
        List of SimSprint objects (not yet persisted to any store).
    """
    if seed is not None:
        random.seed(seed)

    base_date = datetime.utcnow()

    teams = [t.name for t in ctx.teams]

    # Build sprint date windows  ─  each sprint is 2 weeks
    sprint_configs = [
        {
            "id": "sprint-1",
            "name": "Sprint 1 — Foundation",
            "start": base_date - timedelta(days=28),
            "end": base_date - timedelta(days=15),
        },
        {
            "id": "sprint-2",
            "name": "Sprint 2 — Integration",
            "start": base_date - timedelta(days=14),
            "end": base_date - timedelta(days=1),
        },
        {
            "id": "sprint-3",
            "name": "Sprint 3 — Beta Prep",
            "start": base_date,
            "end": base_date + timedelta(days=13),
        },
    ]

    sprints: List[SimSprint] = []
    ticket_counter = 1
    all_ticket_count = 0
    critical_ticket_count = 0

    for sp_cfg in sprint_configs:
        sprint_tickets: List[SimTicket] = []
        # 11-14 tickets per sprint, distributed roughly evenly across teams
        n_tickets = random.randint(11, 14)

        for i in range(n_tickets):
            team = teams[i % len(teams)]
            # Rotate through that team's title pool deterministically
            title_pool = _TITLES_BY_TEAM[team]
            title = title_pool[i % len(title_pool)]

            # Target ≥30% critical path across the whole backlog.
            # Bias: 40% chance each ticket is critical, ensuring we comfortably
            # exceed the 30% floor.
            is_critical = random.random() < 0.40

            ticket_id = f"ATLAS-{ticket_counter:03d}"
            ticket_counter += 1

            t = _make_ticket(
                ticket_id=ticket_id,
                team=team,
                sprint_id=sp_cfg["id"],
                title=title,
                is_critical=is_critical,
                base_date=base_date,
            )
            sprint_tickets.append(t)
            all_ticket_count += 1
            if is_critical:
                critical_ticket_count += 1

        sprints.append(
            SimSprint(
                id=sp_cfg["id"],
                name=sp_cfg["name"],
                start_date=sp_cfg["start"],
                end_date=sp_cfg["end"],
                tickets=sprint_tickets,
            )
        )

    # Safety net: if critical path tickets < 30%, flip some to critical
    if all_ticket_count > 0:
        ratio = critical_ticket_count / all_ticket_count
        if ratio < 0.30:
            for sprint in sprints:
                for ticket in sprint.tickets:
                    if not ticket.is_on_critical_path:
                        ticket.is_on_critical_path = True
                        milestone_id = _SPRINT_MILESTONE_MAP.get(sprint.id)
                        ticket.milestone_target = _MILESTONE_LABELS.get(milestone_id)
                        critical_ticket_count += 1
                        if critical_ticket_count / all_ticket_count >= 0.30:
                            break
                if critical_ticket_count / all_ticket_count >= 0.30:
                    break

    return sprints


def mutate_state(
    sprints: List[SimSprint],
    ctx: ProgramContext,
) -> Tuple[List[SimSprint], List[dict]]:
    """
    Apply one round of stochastic mutations to the simulation state.

    Mutation probabilities come from ctx.simulation_weights so they are
    configurable per program without touching code.

    Mutation types (applied per eligible ticket):
    - TICKET_STALLED      (p=ticket_stalled)   — freeze updated_at, set stale_since
    - TICKET_PROGRESSED   (p=ticket_progressed) — advance IN_PROGRESS→IN_REVIEW,
                                                   IN_REVIEW→DONE
    - BLOCKER_ADDED       (p=blocker_added)     — flip to BLOCKED, add a blocker_id
    - BLOCKER_RESOLVED    (p=blocker_resolved)  — clear blockers, move to IN_PROGRESS
    - SCOPE_EXPANDED      (p=scope_expanded)    — increase story_points by 1-3,
                                                   set scope_changed=True

    Returns:
        (mutated sprints, list of event dicts for the event publisher)
    """
    weights = ctx.simulation_weights
    events: List[dict] = []
    now = datetime.utcnow()

    for sprint in sprints:
        for ticket in sprint.tickets:
            # Skip DONE tickets — they are immutable
            if ticket.status == "DONE":
                continue

            roll = random.random()

            # Evaluate mutations in priority order; only one fires per ticket per cycle
            if ticket.status == "BLOCKED" and roll < weights.blocker_resolved:
                # ── BLOCKER_RESOLVED ────────────────────────────────────────
                ticket.status = "IN_PROGRESS"
                ticket.blocker_ids = []
                ticket.updated_at = now
                events.append({
                    "type": "BLOCKER_RESOLVED",
                    "ticket_id": ticket.id,
                    "sprint_id": sprint.id,
                    "team": ticket.team,
                    "assignee": ticket.assignee,
                })

            elif ticket.status != "BLOCKED" and roll < weights.blocker_added:
                # ── BLOCKER_ADDED ────────────────────────────────────────────
                ticket.status = "BLOCKED"
                # Reference a nearby ticket as the blocker (or self-reference if none)
                blocker_candidates = [
                    t.id for t in sprint.tickets
                    if t.id != ticket.id and t.status not in ("DONE", "BLOCKED")
                ]
                ticket.blocker_ids = (
                    [random.choice(blocker_candidates)] if blocker_candidates else []
                )
                ticket.updated_at = now
                events.append({
                    "type": "BLOCKER_ADDED",
                    "ticket_id": ticket.id,
                    "sprint_id": sprint.id,
                    "team": ticket.team,
                    "assignee": ticket.assignee,
                    "blocker_ids": ticket.blocker_ids,
                })

            elif roll < weights.ticket_stalled:
                # ── TICKET_STALLED ───────────────────────────────────────────
                # Freeze updated_at so Telemetry Agent detects staleness.
                # Set stale_since if not already set (Simulation Engine owns this field).
                stale_cutoff = now - timedelta(
                    days=ctx.thresholds.stale_ticket_days + 1
                )
                ticket.updated_at = stale_cutoff
                if ticket.stale_since is None:
                    ticket.stale_since = stale_cutoff
                events.append({
                    "type": "TICKET_STALLED",
                    "ticket_id": ticket.id,
                    "sprint_id": sprint.id,
                    "team": ticket.team,
                    "assignee": ticket.assignee,
                    "stale_since": ticket.stale_since.isoformat(),
                })

            elif (
                ticket.status in ("IN_PROGRESS", "IN_REVIEW")
                and roll < weights.ticket_stalled + weights.ticket_progressed
            ):
                # ── TICKET_PROGRESSED ─────────────────────────────────────────
                if ticket.status == "IN_PROGRESS":
                    ticket.status = "IN_REVIEW"
                    ticket.points_completed = max(
                        ticket.points_completed,
                        ticket.story_points // 2,
                    )
                else:
                    ticket.status = "DONE"
                    ticket.points_completed = ticket.story_points
                ticket.updated_at = now
                events.append({
                    "type": "TICKET_PROGRESSED",
                    "ticket_id": ticket.id,
                    "sprint_id": sprint.id,
                    "new_status": ticket.status,
                    "team": ticket.team,
                    "assignee": ticket.assignee,
                })

            elif roll < weights.ticket_stalled + weights.ticket_progressed + weights.scope_expanded:
                # ── SCOPE_EXPANDED ────────────────────────────────────────────
                increase = random.randint(1, 3)
                ticket.story_points += increase
                ticket.scope_changed = True
                ticket.updated_at = now
                events.append({
                    "type": "SCOPE_EXPANDED",
                    "ticket_id": ticket.id,
                    "sprint_id": sprint.id,
                    "team": ticket.team,
                    "assignee": ticket.assignee,
                    "points_added": increase,
                    "new_story_points": ticket.story_points,
                })

    return sprints, events
