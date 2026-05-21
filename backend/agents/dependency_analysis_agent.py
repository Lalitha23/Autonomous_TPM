"""
Dependency Analysis Agent — Stage 5.

Three pure graph-utility functions + one async LangGraph node.

The agent only fires when BLOCKED risk flags exist in state. It:
1. Builds an adjacency graph from the ticket list.
2. Traces blocker chains upstream from each blocked ticket.
3. Calculates blast radius (downstream count).
4. Elevates severity when chain depth > 1.
5. Calls Claude API to generate a causal reasoning narrative per chain.
6. Returns enriched risk_flags with dependency metadata.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections import deque
from typing import List, Optional

from agents.state import PipelineState
from db.models import AgentDecision
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

_MAX_CHAIN_DEPTH = 10
_SEVERITY = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


def _bump(severity: str) -> str:
    idx = _SEVERITY.index(severity)
    return _SEVERITY[min(idx + 1, len(_SEVERITY) - 1)]


# ── Pure graph utility functions ──────────────────────────────────────────────

def build_dependency_graph(tickets: List[dict]) -> dict:
    """
    Build an adjacency dict mapping each ticket to its list of blockers.

    Only non-DONE tickets are included.

    Returns:
        {ticket_id: [blocking_ticket_ids, ...]}
    """
    graph: dict[str, List[str]] = {}
    for t in tickets:
        if t.get("status") == "DONE":
            continue
        graph[t["id"]] = list(t.get("blocker_ids") or [])
    return graph


def trace_chain(
    ticket_id: str,
    graph: dict,
    visited: Optional[set] = None,
    _depth: int = 0,
) -> List[str]:
    """
    Trace the upstream blocker chain from a given ticket.

    Stops at depth _MAX_CHAIN_DEPTH or when a cycle is detected.

    Args:
        ticket_id: Starting ticket (typically a BLOCKED one).
        graph:     Adjacency dict from build_dependency_graph().
        visited:   Set of already-visited ticket IDs (cycle guard).
        _depth:    Current recursion depth (internal).

    Returns:
        Ordered list of ticket IDs in the blocker chain, starting from
        the immediate blocker(s) up to the root. Does NOT include ticket_id.
    """
    if visited is None:
        visited = set()
    if ticket_id in visited or _depth >= _MAX_CHAIN_DEPTH:
        return []

    visited = visited | {ticket_id}
    blockers = graph.get(ticket_id, [])

    chain: List[str] = []
    for blocker_id in blockers:
        if blocker_id not in visited:
            chain.append(blocker_id)
            chain.extend(trace_chain(blocker_id, graph, visited, _depth + 1))
    return chain


def calculate_blast_radius(ticket_id: str, graph: dict) -> int:
    """
    Count how many tickets are downstream of ticket_id
    (i.e., are blocked by it directly or transitively).

    Returns the count of downstream tickets, not including ticket_id itself.
    """
    # Build reverse graph: {blocker_id: [tickets_it_blocks]}
    reverse: dict[str, List[str]] = {}
    for blocked, blockers in graph.items():
        for blocker in blockers:
            reverse.setdefault(blocker, []).append(blocked)

    visited: set[str] = set()
    queue: deque[str] = deque(reverse.get(ticket_id, []))
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        queue.extend(d for d in reverse.get(current, []) if d not in visited)

    return len(visited)


def _find_milestone_threatened(
    ticket_id: str,
    graph: dict,
    ticket_by_id: dict[str, dict],
) -> Optional[str]:
    """
    Find the milestone_target of the furthest downstream critical-path ticket.
    Returns None if no critical-path ticket is downstream.
    """
    reverse: dict[str, List[str]] = {}
    for blocked, blockers in graph.items():
        for blocker in blockers:
            reverse.setdefault(blocker, []).append(blocked)

    visited: set[str] = set()
    queue: deque[str] = deque(reverse.get(ticket_id, []))
    last_milestone: Optional[str] = None

    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        t = ticket_by_id.get(current)
        if t and t.get("is_on_critical_path") and t.get("milestone_target"):
            last_milestone = t["milestone_target"]
        queue.extend(d for d in reverse.get(current, []) if d not in visited)

    return last_milestone


# ── Claude API causal reasoning ───────────────────────────────────────────────

def _build_claude_prompt(
    chain_details: List[dict],
    ctx_dict: dict,
) -> str:
    program_name = ctx_dict.get("program_name", "the program")
    domain = ctx_dict.get("domain", "software")

    lines = [
        f"You are a Senior Technical Program Manager analysing blocked tickets "
        f"in {program_name} ({domain}).",
        "",
        "For each dependency chain below, write 2-3 sentences explaining:",
        "1. What is blocked and why.",
        "2. Why it matters (impact on sprint or milestone).",
        "3. What the downstream impact is.",
        "",
        "Return ONLY a JSON array. No markdown, no preamble.",
        'Format: [{"ticket_id": "ATLAS-XXX", "causal_explanation": "..."}]',
        "",
        "Dependency chains:",
    ]

    for cd in chain_details:
        lines.append(
            f"- Ticket {cd['ticket_id']} \"{cd['title']}\" "
            f"(Team: {cd['team']}, Priority: {cd['priority']}) "
            f"is blocked by chain: {' → '.join(cd['chain']) or 'unknown'}. "
            f"Blast radius: {cd['blast_radius']} downstream ticket(s). "
            f"Milestone threatened: {cd['milestone_threatened'] or 'none'}."
        )

    return "\n".join(lines)


async def _call_claude(prompt: str) -> List[dict]:
    """
    Call the Claude API and parse the JSON response.
    Returns [] on any failure (API error, bad JSON, missing key).
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set — skipping causal reasoning enrichment."
        )
        return []

    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=api_key)
        model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        max_tokens = int(os.getenv("CLAUDE_MAX_TOKENS", "4096"))

        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=(
                "You are an expert TPM. Always respond with valid JSON only — "
                "no markdown, no explanation outside the JSON array."
            ),
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        # Strip optional markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        return json.loads(raw)

    except Exception:
        logger.exception("Claude API call failed in dependency analysis agent.")
        return []


# ── Agent node ────────────────────────────────────────────────────────────────

async def run_dependency_analysis_agent(state: PipelineState) -> dict:
    """
    LangGraph node: Dependency Analysis Agent.

    Only runs if BLOCKED risk flags exist. Enriches each BLOCKED flag with
    chain metadata and Claude-generated causal reasoning.
    """
    risk_flags = state.get("risk_flags", [])
    blocked_flags = [f for f in risk_flags if f.get("flag") == "BLOCKED"]
    run_id = state.get("run_id", "unknown")
    cycle_number = state.get("cycle_number", 0)
    program_id_str = state.get("program_id", "")
    ctx_dict = state.get("program_context", {})
    domain = ctx_dict.get("domain", "unknown")

    if not blocked_flags:
        # No BLOCKED flags — log a no-op decision and pass through unchanged
        try:
            program_uuid = uuid.UUID(program_id_str)
            async with AsyncSessionLocal() as session:
                session.add(AgentDecision(
                    program_id=program_uuid,
                    domain=domain,
                    run_id=run_id,
                    cycle_number=cycle_number,
                    agent_name="dependency_analysis",
                    decision="No BLOCKED flags this cycle — dependency analysis skipped.",
                    reasoning="0 BLOCKED flags in risk_flags; chain analysis not required.",
                    input_summary={"program_id": program_id_str, "cycle_number": cycle_number, "blocked_flag_count": 0},
                    output_summary={"enriched_count": 0, "chains": []},
                ))
                await session.commit()
        except Exception:
            pass  # non-critical — don't fail the pipeline
        return {}

    tickets = state.get("tickets", [])
    run_id = state["run_id"]
    cycle_number = state["cycle_number"]
    program_id_str = state["program_id"]
    ctx_dict = state["program_context"]
    program_uuid = uuid.UUID(program_id_str)
    domain = ctx_dict.get("domain", "unknown")

    ticket_by_id: dict[str, dict] = {t["id"]: t for t in tickets}
    graph = build_dependency_graph(tickets)

    # ── Enrich each BLOCKED flag ──────────────────────────────────────────
    enriched: List[dict] = []
    chain_details_for_claude: List[dict] = []

    for flag in blocked_flags:
        tid = flag["ticket_id"]
        t = ticket_by_id.get(tid, {})

        chain = trace_chain(tid, graph)
        chain_depth = len(chain)
        blast_radius = calculate_blast_radius(tid, graph)
        milestone_threatened = _find_milestone_threatened(tid, graph, ticket_by_id)

        # Elevate severity if chain depth > 1
        severity = flag["severity"]
        if chain_depth > 1:
            severity = _bump(severity)

        enriched_flag = {
            **flag,
            "severity": severity,
            "dependency_chain": chain,
            "chain_depth": chain_depth,
            "blast_radius": blast_radius,
            "milestone_threatened": milestone_threatened,
            "causal_explanation": None,  # filled by Claude below
        }
        enriched.append(enriched_flag)

        chain_details_for_claude.append({
            "ticket_id": tid,
            "title": t.get("title", "Unknown"),
            "team": t.get("team", "Unknown"),
            "priority": t.get("priority", "P3"),
            "chain": chain,
            "blast_radius": blast_radius,
            "milestone_threatened": milestone_threatened,
        })

    # ── Claude causal reasoning ────────────────────────────────────────────
    if chain_details_for_claude:
        prompt = _build_claude_prompt(chain_details_for_claude, ctx_dict)
        explanations = await _call_claude(prompt)

        # Map ticket_id → causal_explanation
        explanation_map: dict[str, str] = {
            e["ticket_id"]: e["causal_explanation"]
            for e in explanations
            if isinstance(e, dict) and "ticket_id" in e and "causal_explanation" in e
        }

        for ef in enriched:
            tid = ef["ticket_id"]
            if tid in explanation_map:
                ef["causal_explanation"] = explanation_map[tid]
            elif ef["causal_explanation"] is None:
                ef["causal_explanation"] = (
                    f"Ticket {tid} is blocked by a {ef['chain_depth']}-hop chain "
                    f"affecting {ef['blast_radius']} downstream ticket(s)."
                )

    # ── Merge enriched BLOCKED flags back into risk_flags list ────────────
    enriched_by_id: dict[str, dict] = {e["ticket_id"]: e for e in enriched}
    updated_risk_flags: List[dict] = []
    for f in risk_flags:
        if f["flag"] == "BLOCKED" and f["ticket_id"] in enriched_by_id:
            updated_risk_flags.append(enriched_by_id[f["ticket_id"]])
        else:
            updated_risk_flags.append(f)

    # ── Build decision entry ──────────────────────────────────────────────
    chains_summary = "; ".join(
        f"{e['ticket_id']} (depth={e['chain_depth']}, radius={e['blast_radius']})"
        for e in enriched
    )
    decision_text = (
        f"Analysed {len(enriched)} blocked ticket(s). "
        f"Dependency chains: {chains_summary}."
    )
    reasoning_text = " | ".join(
        f"{e['ticket_id']}: chain={e['dependency_chain']}, "
        f"milestone={e['milestone_threatened'] or 'none'}"
        for e in enriched
    )

    decision_dict = {
        "agent": "dependency_analysis",
        "decision": decision_text,
        "reasoning": reasoning_text,
        "input_summary": {
            "program_id": program_id_str,
            "cycle_number": cycle_number,
            "blocked_flag_count": len(blocked_flags),
        },
        "output_summary": {
            "enriched_count": len(enriched),
            "chains": [
                {"ticket_id": e["ticket_id"], "depth": e["chain_depth"],
                 "radius": e["blast_radius"]}
                for e in enriched
            ],
        },
    }

    async with AsyncSessionLocal() as session:
        session.add(
            AgentDecision(
                program_id=program_uuid,
                domain=domain,
                run_id=run_id,
                cycle_number=cycle_number,
                agent_name="dependency_analysis",
                decision=decision_text,
                reasoning=reasoning_text,
                input_summary=decision_dict["input_summary"],
                output_summary=decision_dict["output_summary"],
            )
        )
        await session.commit()

    logger.info(
        "Dependency Analysis [run=%s cycle=%d]: %d blocked chains analysed",
        run_id[:8],
        cycle_number,
        len(enriched),
    )

    existing_decisions = list(state.get("agent_decisions", []))
    return {
        "risk_flags": updated_risk_flags,
        "agent_decisions": existing_decisions + [decision_dict],
    }
