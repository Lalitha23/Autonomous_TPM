"""
Risk Detection Agent — Stage 5.

Four deterministic, rule-based detection functions (no Claude API).
Each function receives the serialised ticket list from PipelineState and
returns a list of risk flag dicts.

Severity ladder (lowest → highest):  LOW → MEDIUM → HIGH → CRITICAL
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import update

from agents.state import PipelineState
from core.program_context import ProgramContext
from db.models import AgentDecision, Sprint, Ticket
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# Severity ordering — used for comparisons and bumping
_SEVERITY = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


def _bump(severity: str) -> str:
    """Bump severity one level up, capping at CRITICAL."""
    idx = _SEVERITY.index(severity)
    return _SEVERITY[min(idx + 1, len(_SEVERITY) - 1)]


def _sev_gt(a: str, b: str) -> bool:
    """Return True if severity a is strictly higher than b."""
    return _SEVERITY.index(a) > _SEVERITY.index(b)


def _badge_for_worst(worst: Optional[str]) -> str:
    """Map worst severity to sprint health badge."""
    if worst is None:
        return "HEALTHY"
    if worst in ("LOW", "MEDIUM"):
        return "WATCH"
    if worst == "HIGH":
        return "ALERT"
    return "ESCALATE"  # CRITICAL


# ── Detection functions ───────────────────────────────────────────────────────

def detect_stale_tickets(
    tickets: List[dict],
    context: ProgramContext,
) -> List[dict]:
    """
    Flag tickets whose stale_since exceeds the configured threshold.

    Severity computation:
    1. Base from days stale:
         days < threshold*2  → LOW
         days >= threshold*2 → MEDIUM
    2. If is_on_critical_path (and multiplier > 1.0): bump one level.
    3. If priority P0 or P1: bump one more level.
    """
    threshold = context.thresholds.stale_ticket_days
    multiplier = context.thresholds.critical_path_severity_multiplier
    now = datetime.now(timezone.utc)
    flags: List[dict] = []

    for ticket in tickets:
        stale_since_raw = ticket.get("stale_since")
        if stale_since_raw is None:
            continue

        stale_since = datetime.fromisoformat(stale_since_raw)
        if stale_since.tzinfo is None:
            stale_since = stale_since.replace(tzinfo=timezone.utc)
        days_stale = (now - stale_since).days

        if days_stale < threshold:
            continue

        # Step 1 — base severity
        severity = "LOW" if days_stale < threshold * 2 else "MEDIUM"

        # Step 2 — critical path multiplier
        if ticket.get("is_on_critical_path") and multiplier > 1.0:
            severity = _bump(severity)

        # Step 3 — priority bump
        if ticket.get("priority") in ("P0", "P1"):
            severity = _bump(severity)

        flags.append({
            "ticket_id": ticket["id"],
            "flag": "STALE",
            "severity": severity,
            "reason": (
                f"Ticket has been stale for {days_stale} day(s) "
                f"(threshold: {threshold} days). "
                f"{'On critical path. ' if ticket.get('is_on_critical_path') else ''}"
                f"Priority: {ticket.get('priority', 'unknown')}."
            ),
        })

    return flags


def detect_blocked_tickets(
    tickets: List[dict],
    context: ProgramContext,
) -> List[dict]:
    """
    Flag tickets that have active (non-DONE) blockers.

    Severity:
    - Base: MEDIUM
    - Critical path: MEDIUM → HIGH
    - P0: CRITICAL (unconditional)
    - P1 + critical path: CRITICAL
    """
    ticket_by_id = {t["id"]: t for t in tickets}
    flags: List[dict] = []

    for ticket in tickets:
        blocker_ids = ticket.get("blocker_ids") or []
        if not blocker_ids:
            continue

        active_blockers = [
            bid for bid in blocker_ids
            if bid in ticket_by_id
            and ticket_by_id[bid].get("status") != "DONE"
        ]
        if not active_blockers:
            continue

        severity = "MEDIUM"
        is_critical = ticket.get("is_on_critical_path", False)
        priority = ticket.get("priority", "P3")

        if is_critical:
            severity = "HIGH"

        if priority == "P0":
            severity = "CRITICAL"
        elif priority == "P1" and is_critical:
            severity = "CRITICAL"

        flags.append({
            "ticket_id": ticket["id"],
            "flag": "BLOCKED",
            "severity": severity,
            "reason": (
                f"Ticket is blocked by {len(active_blockers)} unresolved "
                f"blocker(s): {', '.join(active_blockers)}. "
                f"{'On critical path. ' if is_critical else ''}"
                f"Priority: {priority}."
            ),
        })

    return flags


def detect_scope_creep(
    tickets: List[dict],
    context: ProgramContext,
) -> List[dict]:
    """
    Flag tickets where scope_changed = True.

    Severity:
    - Base: LOW
    - If story_points >= threshold*2 (proxy for significant growth): MEDIUM
      (threshold = scope_creep_story_point_increase; multiplied by 2 and
      scaled to points — tickets above 8 points are treated as significant)
    - If is_on_critical_path: bump one level.

    Note: Original story points are not tracked; current story_points are used
    as a proxy for scope size.
    """
    threshold = context.thresholds.scope_creep_story_point_increase
    # Significant size proxy: story_points >= 8 (typical large ticket)
    significant_point_threshold = max(8, int(threshold * 2 * 10))
    multiplier = context.thresholds.critical_path_severity_multiplier
    flags: List[dict] = []

    for ticket in tickets:
        if not ticket.get("scope_changed"):
            continue

        severity = "LOW"
        story_points = ticket.get("story_points", 0)

        if story_points >= significant_point_threshold:
            severity = "MEDIUM"

        if ticket.get("is_on_critical_path") and multiplier > 1.0:
            severity = _bump(severity)

        flags.append({
            "ticket_id": ticket["id"],
            "flag": "SCOPE_CREEP",
            "severity": severity,
            "reason": (
                f"Ticket scope has expanded (currently {story_points} story "
                f"point(s)). "
                f"{'On critical path. ' if ticket.get('is_on_critical_path') else ''}"
                f"Priority: {ticket.get('priority', 'unknown')}."
            ),
        })

    return flags


def detect_overloaded_assignees(
    tickets: List[dict],
    context: ProgramContext,
) -> List[dict]:
    """
    Flag IN_PROGRESS tickets whose assignee exceeds overload_points_per_assignee.

    Severity:
    - Base: MEDIUM
    - HIGH if the assignee has any IN_PROGRESS ticket on the critical path.
    """
    threshold = context.thresholds.overload_points_per_assignee

    # Aggregate IN_PROGRESS load per assignee
    assignee_tickets: dict[str, List[dict]] = {}
    for t in tickets:
        if t.get("status") == "IN_PROGRESS":
            assignee = t.get("assignee") or "unknown"
            assignee_tickets.setdefault(assignee, []).append(t)

    flags: List[dict] = []

    for assignee, in_progress in assignee_tickets.items():
        total_points = sum(t.get("story_points", 0) for t in in_progress)
        if total_points <= threshold:
            continue

        any_critical = any(t.get("is_on_critical_path") for t in in_progress)
        severity = "HIGH" if any_critical else "MEDIUM"

        for ticket in in_progress:
            flags.append({
                "ticket_id": ticket["id"],
                "flag": "OVERLOADED",
                "severity": severity,
                "reason": (
                    f"Assignee {assignee} has {total_points} story points in "
                    f"progress (threshold: {threshold}). "
                    f"{'Has critical path work. ' if any_critical else ''}"
                    f"Ticket contributes {ticket.get('story_points', 0)} pt(s)."
                ),
            })

    return flags


# ── Sprint health computation ─────────────────────────────────────────────────

def _compute_sprint_health(
    risk_flags: List[dict],
    sprint_summaries: List[dict],
    ticket_sprint_map: dict[str, str],
    run_id: str,
) -> List[dict]:
    """
    Determine health badge per sprint from risk flags.

    Returns a list of sprint_health dicts:
        {sprint_id, name, health_badge, worst_severity, flagged_ticket_count}
    """
    # Map sprint_id → worst severity seen so far
    sprint_worst: dict[str, Optional[str]] = {
        s["sprint_id"]: None for s in sprint_summaries
    }
    sprint_flag_count: dict[str, int] = {s["sprint_id"]: 0 for s in sprint_summaries}

    for flag in risk_flags:
        tid = flag["ticket_id"]
        sprint_id = ticket_sprint_map.get(tid)
        if sprint_id is None or sprint_id not in sprint_worst:
            continue

        sprint_flag_count[sprint_id] += 1
        current_worst = sprint_worst[sprint_id]
        flag_sev = flag["severity"]

        if current_worst is None or _sev_gt(flag_sev, current_worst):
            sprint_worst[sprint_id] = flag_sev

    sprint_name_map = {s["sprint_id"]: s["name"] for s in sprint_summaries}

    health_list: List[dict] = []
    for sprint_id, worst_sev in sprint_worst.items():
        health_list.append({
            "sprint_id": sprint_id,
            "name": sprint_name_map.get(sprint_id, sprint_id),
            "health_badge": _badge_for_worst(worst_sev),
            "worst_severity": worst_sev,
            "flagged_ticket_count": sprint_flag_count[sprint_id],
            "last_run_id": run_id,
        })

    return health_list


# ── Agent node ────────────────────────────────────────────────────────────────

async def run_risk_detection_agent(state: PipelineState) -> dict:
    """
    LangGraph node: Risk Detection Agent.

    Runs all 4 detectors, merges results, computes sprint health,
    persists to PostgreSQL, and returns partial state update.
    """
    tickets = state.get("tickets", [])
    sprint_summaries = state.get("sprint_summaries", [])
    run_id = state["run_id"]
    cycle_number = state["cycle_number"]
    program_id_str = state["program_id"]
    ctx_dict = state["program_context"]
    program_uuid = uuid.UUID(program_id_str)
    domain = ctx_dict.get("domain", "unknown")

    # Rebuild ProgramContext from the serialised dict in state
    from core.program_context import (
        ProgramContext, ThresholdConfig, SprintHealthRules,
        EscalationRules, BaselineConfig, SimulationWeights,
        TeamConfig, MilestoneConfig,
    )
    from datetime import date as _date

    ctx = ProgramContext(**{
        **ctx_dict,
        "launch_target": _date.fromisoformat(ctx_dict["launch_target"])
        if isinstance(ctx_dict.get("launch_target"), str) else ctx_dict["launch_target"],
        "critical_path": [
            {**m, "target_date": _date.fromisoformat(m["target_date"])
             if isinstance(m.get("target_date"), str) else m["target_date"]}
            for m in ctx_dict.get("critical_path", [])
        ],
    })

    # ── Run all 4 detectors ───────────────────────────────────────────────
    stale_flags    = detect_stale_tickets(tickets, ctx)
    blocked_flags  = detect_blocked_tickets(tickets, ctx)
    scope_flags    = detect_scope_creep(tickets, ctx)
    overload_flags = detect_overloaded_assignees(tickets, ctx)

    all_risk_flags = stale_flags + blocked_flags + scope_flags + overload_flags

    n_flags = len(all_risk_flags)
    n_affected = len({f["ticket_id"] for f in all_risk_flags})

    # ── Build ticket → sprint lookup ──────────────────────────────────────
    ticket_sprint_map: dict[str, str] = {
        t["id"]: t.get("sprint_id", "") for t in tickets
    }

    # ── Compute sprint health ─────────────────────────────────────────────
    sprint_health = _compute_sprint_health(
        all_risk_flags, sprint_summaries, ticket_sprint_map, run_id
    )

    # ── Build per-ticket highest-severity flag (for DB + state update) ────
    top_flag_by_ticket: dict[str, dict] = {}
    for flag in all_risk_flags:
        tid = flag["ticket_id"]
        if tid not in top_flag_by_ticket:
            top_flag_by_ticket[tid] = flag
        elif _sev_gt(flag["severity"], top_flag_by_ticket[tid]["severity"]):
            top_flag_by_ticket[tid] = flag

    # ── Update tickets in state with risk info ────────────────────────────
    updated_tickets: List[dict] = []
    for t in tickets:
        top = top_flag_by_ticket.get(t["id"])
        updated_tickets.append({
            **t,
            "risk_flag":     top["flag"]     if top else None,
            "risk_severity": top["severity"] if top else None,
            "risk_reason":   top["reason"]   if top else None,
        })

    # ── Persist risk flags to PostgreSQL tickets ──────────────────────────
    async with AsyncSessionLocal() as session:
        for t in updated_tickets:
            await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == t["id"],
                    Ticket.program_id == program_uuid,
                )
                .values(
                    risk_flag=t["risk_flag"],
                    risk_severity=t["risk_severity"],
                    risk_reason=t["risk_reason"],
                )
            )

        # Persist sprint health
        for sh in sprint_health:
            await session.execute(
                update(Sprint)
                .where(
                    Sprint.id == sh["sprint_id"],
                    Sprint.program_id == program_uuid,
                )
                .values(
                    health_badge=sh["health_badge"],
                    worst_severity=sh["worst_severity"],
                    last_run_id=sh["last_run_id"],
                )
            )

        await session.commit()

    # ── Build decision entry ──────────────────────────────────────────────
    health_summary = ", ".join(
        f"{sh['name']}: {sh['health_badge']}" for sh in sprint_health
    )
    decision_text = (
        f"{n_flags} risk flag(s) detected across {n_affected} ticket(s). "
        f"Sprint health: {health_summary}."
    )

    # Detailed reasoning: one line per flag type
    flag_lines = []
    if stale_flags:
        flag_lines.append(
            f"STALE ({len(stale_flags)}): tickets with stale_since >= "
            f"{ctx.thresholds.stale_ticket_days} days threshold."
        )
    if blocked_flags:
        flag_lines.append(
            f"BLOCKED ({len(blocked_flags)}): tickets with unresolved blockers."
        )
    if scope_flags:
        flag_lines.append(
            f"SCOPE_CREEP ({len(scope_flags)}): tickets with scope_changed=True."
        )
    if overload_flags:
        flag_lines.append(
            f"OVERLOADED ({len(overload_flags)}): assignee(s) exceeding "
            f"{ctx.thresholds.overload_points_per_assignee} point threshold."
        )
    if not flag_lines:
        flag_lines.append("No risk flags detected this cycle.")

    reasoning_text = " | ".join(flag_lines)

    decision_dict = {
        "agent": "risk_detection",
        "decision": decision_text,
        "reasoning": reasoning_text,
        "input_summary": {
            "program_id": program_id_str,
            "cycle_number": cycle_number,
            "ticket_count": len(tickets),
        },
        "output_summary": {
            "flag_count": n_flags,
            "affected_tickets": n_affected,
            "sprint_health": {sh["sprint_id"]: sh["health_badge"] for sh in sprint_health},
        },
    }

    async with AsyncSessionLocal() as session:
        session.add(
            AgentDecision(
                program_id=program_uuid,
                domain=domain,
                run_id=run_id,
                cycle_number=cycle_number,
                agent_name="risk_detection",
                decision=decision_text,
                reasoning=reasoning_text,
                input_summary=decision_dict["input_summary"],
                output_summary=decision_dict["output_summary"],
            )
        )
        await session.commit()

    logger.info(
        "Risk Detection [run=%s cycle=%d]: %d flags, %d affected tickets",
        run_id[:8],
        cycle_number,
        n_flags,
        n_affected,
    )

    existing_decisions = list(state.get("agent_decisions", []))
    return {
        "tickets": updated_tickets,
        "risk_flags": all_risk_flags,
        "sprint_health": sprint_health,
        "agent_decisions": existing_decisions + [decision_dict],
    }
