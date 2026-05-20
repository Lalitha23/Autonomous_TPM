"""
Mitigation Agent — Stage 6.

Rule-based only (no Claude API). Receives risk_flags from state and produces
a list of mitigation dicts, one per flag. HIGH/CRITICAL flags where the flag
type is BLOCKED or STALE are marked requires_escalation=True and persisted to
the PostgreSQL `escalations` table.

Urgency rules:
  CRITICAL / HIGH  → IMMEDIATE
  MEDIUM           → THIS_SPRINT
  LOW              → NEXT_SPRINT

requires_escalation = True iff:
  severity in {HIGH, CRITICAL}  AND  flag in {BLOCKED, STALE}
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List

from agents.state import PipelineState
from db.models import AgentDecision, Escalation
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

_SEVERITY = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
_ESCALATION_SEVERITIES = {"HIGH", "CRITICAL"}
_ESCALATION_FLAGS = {"BLOCKED", "STALE"}


def _urgency(severity: str) -> str:
    if severity in ("CRITICAL", "HIGH"):
        return "IMMEDIATE"
    if severity == "MEDIUM":
        return "THIS_SPRINT"
    return "NEXT_SPRINT"


def _action_for_flag(flag: dict, ticket_by_id: dict, ctx_dict: dict) -> tuple[str, str | None]:
    """
    Build (action_text, suggested_owner) for a single risk flag.

    Returns a human-readable action string and an optional suggested owner.
    """
    tid = flag["ticket_id"]
    severity = flag["severity"]
    flag_type = flag["flag"]
    t = ticket_by_id.get(tid, {})

    assignee = t.get("assignee") or "the current assignee"
    team = t.get("team") or "the owning team"
    priority = t.get("priority", "P3")

    # Find team lead from ctx
    team_lead: str | None = None
    for team_cfg in ctx_dict.get("teams", []):
        if team_cfg.get("name") == team:
            team_lead = team_cfg.get("lead")
            break

    if flag_type == "BLOCKED":
        blocker_ids: list = t.get("blocker_ids") or []
        blocker_list = ", ".join(blocker_ids) if blocker_ids else "unknown blockers"
        action = (
            f"Resolve blocker(s) blocking {tid}: {blocker_list}. "
            f"Assign {assignee} ({team}) to coordinate unblock. "
            f"Escalate to {team_lead or 'team lead'} if not resolved within 24 hours."
        )
        suggested_owner = team_lead or assignee

    elif flag_type == "STALE":
        stale_since_raw = t.get("stale_since")
        if stale_since_raw:
            stale_since = datetime.fromisoformat(stale_since_raw)
            if stale_since.tzinfo is None:
                stale_since = stale_since.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - stale_since).days
            days_str = f"{days} day(s)"
        else:
            days_str = "multiple days"

        action = (
            f"Ticket {tid} has had no progress for {days_str}. "
            f"Schedule a sync with {assignee} ({team}) to identify blockers or re-assign. "
            f"{'Immediate attention required — on critical path. ' if t.get('is_on_critical_path') else ''}"
            f"Update status or points_completed by end of day."
        )
        suggested_owner = team_lead or assignee

    elif flag_type == "SCOPE_CREEP":
        story_points = t.get("story_points", 0)
        action = (
            f"Ticket {tid} has expanded scope ({story_points} story points). "
            f"Review with {team_lead or 'team lead'} ({team}) to decide: "
            f"split into sub-tickets, defer non-critical work, or adjust sprint commitment. "
            f"Update milestone_target if timeline is affected."
        )
        suggested_owner = team_lead

    elif flag_type == "OVERLOADED":
        threshold = ctx_dict.get("thresholds", {}).get("overload_points_per_assignee", 18)
        action = (
            f"Assignee {assignee} is overloaded (exceeds {threshold} story points in progress). "
            f"Redistribute at least one IN_PROGRESS ticket to another {team} team member. "
            f"Coordinate with {team_lead or 'team lead'} on priority ordering."
        )
        suggested_owner = team_lead or assignee

    else:
        action = f"Review and address {flag_type} risk for ticket {tid} ({severity} severity)."
        suggested_owner = assignee

    return action, suggested_owner


def compute_mitigations(
    risk_flags: List[dict],
    tickets: List[dict],
    ctx_dict: dict,
) -> List[dict]:
    """
    Pure function: build mitigation dicts from risk flags.

    Returns a list of mitigation dicts with keys:
        ticket_id, flag, severity, urgency, action,
        suggested_owner, requires_escalation
    """
    ticket_by_id = {t["id"]: t for t in tickets}
    mitigations: List[dict] = []

    for flag in risk_flags:
        severity = flag["severity"]
        flag_type = flag["flag"]
        urgency = _urgency(severity)
        requires_escalation = (
            severity in _ESCALATION_SEVERITIES and flag_type in _ESCALATION_FLAGS
        )
        action, suggested_owner = _action_for_flag(flag, ticket_by_id, ctx_dict)

        mitigations.append({
            "ticket_id": flag["ticket_id"],
            "flag": flag_type,
            "severity": severity,
            "urgency": urgency,
            "action": action,
            "suggested_owner": suggested_owner,
            "requires_escalation": requires_escalation,
        })

    return mitigations


# ── Agent node ────────────────────────────────────────────────────────────────

async def run_mitigation_agent(state: PipelineState) -> dict:
    """
    LangGraph node: Mitigation Agent.

    Computes mitigations for all risk flags. Persists HIGH/CRITICAL
    BLOCKED/STALE flags to the escalations table.
    """
    risk_flags = state.get("risk_flags", [])
    tickets = state.get("tickets", [])
    run_id = state["run_id"]
    cycle_number = state["cycle_number"]
    program_id_str = state["program_id"]
    ctx_dict = state["program_context"]
    program_uuid = uuid.UUID(program_id_str)
    domain = ctx_dict.get("domain", "unknown")

    if not risk_flags:
        logger.info("Mitigation [run=%s cycle=%d]: no risk flags — skipping.", run_id[:8], cycle_number)
        return {"mitigations": []}

    mitigations = compute_mitigations(risk_flags, tickets, ctx_dict)

    # ── Persist escalations ───────────────────────────────────────────────
    escalation_rows = [m for m in mitigations if m["requires_escalation"]]
    n_escalations = len(escalation_rows)

    async with AsyncSessionLocal() as session:
        for m in escalation_rows:
            session.add(
                Escalation(
                    program_id=program_uuid,
                    domain=domain,
                    run_id=run_id,
                    ticket_id=m["ticket_id"],
                    flag=m["flag"],
                    severity=m["severity"],
                    action=m["action"],
                    suggested_owner=m["suggested_owner"],
                    urgency=m["urgency"],
                )
            )

        # ── Persist agent decision ────────────────────────────────────────
        immediate_count = sum(1 for m in mitigations if m["urgency"] == "IMMEDIATE")
        decision_text = (
            f"{len(mitigations)} mitigation(s) generated for {len(risk_flags)} risk flag(s). "
            f"{n_escalations} escalation(s) persisted. "
            f"{immediate_count} require(s) immediate action."
        )
        reasoning_lines = []
        for m in mitigations:
            reasoning_lines.append(
                f"{m['ticket_id']} [{m['flag']}/{m['severity']}] → "
                f"{m['urgency']}; escalate={m['requires_escalation']}"
            )
        reasoning_text = " | ".join(reasoning_lines) if reasoning_lines else "No flags to mitigate."

        session.add(
            AgentDecision(
                program_id=program_uuid,
                domain=domain,
                run_id=run_id,
                cycle_number=cycle_number,
                agent_name="mitigation",
                decision=decision_text,
                reasoning=reasoning_text,
                input_summary={
                    "program_id": program_id_str,
                    "cycle_number": cycle_number,
                    "flag_count": len(risk_flags),
                },
                output_summary={
                    "mitigation_count": len(mitigations),
                    "escalation_count": n_escalations,
                    "immediate_count": immediate_count,
                },
            )
        )
        await session.commit()

    decision_dict = {
        "agent": "mitigation",
        "decision": decision_text,
        "reasoning": reasoning_text,
        "input_summary": {
            "program_id": program_id_str,
            "cycle_number": cycle_number,
            "flag_count": len(risk_flags),
        },
        "output_summary": {
            "mitigation_count": len(mitigations),
            "escalation_count": n_escalations,
            "immediate_count": immediate_count,
        },
    }

    logger.info(
        "Mitigation [run=%s cycle=%d]: %d mitigations, %d escalations",
        run_id[:8],
        cycle_number,
        len(mitigations),
        n_escalations,
    )

    existing_decisions = list(state.get("agent_decisions", []))
    return {
        "mitigations": mitigations,
        "agent_decisions": existing_decisions + [decision_dict],
    }
