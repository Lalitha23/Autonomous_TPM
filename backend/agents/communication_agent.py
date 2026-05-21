"""
Communication Agent — Stage 6.

Generates three executive-quality TPM outputs via Claude API:
  1. STANDUP_SUMMARY   — 3-paragraph prose, no bullets
  2. ESCALATION_MEMO   — VP-ready one-page memo (only when escalations exist)
  3. RISK_DIGEST       — one line per risk, sorted CRITICAL → LOW

Falls back to high-quality programmatic templates when ANTHROPIC_API_KEY is
absent or empty — output must be indistinguishable from AI-written copy in
tone and completeness.

After generating outputs:
  - Persists each to PostgreSQL `executive_outputs` table
  - Updates Redis cache keys: tickets:current:{program_id} and
    sprint_health:current:{program_id}
  - Publishes a completion event to Redis Stream `pipeline:{program_id}`
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import redis.asyncio as aioredis

from agents.state import PipelineState
from db.models import AgentDecision, ExecutiveOutput
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]


# ── Prompt builders ───────────────────────────────────────────────────────────

def _build_standup_prompt(
    sprint_health: List[dict],
    risk_flags: List[dict],
    mitigations: List[dict],
    tickets: List[dict],
    ctx_dict: dict,
) -> str:
    program_name = ctx_dict.get("program_name", "the program")
    domain = ctx_dict.get("domain", "software")
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    flag_summary = _summarize_flags(risk_flags)
    health_lines = [
        f"  - {sh['name']}: {sh['health_badge']} "
        f"(worst severity: {sh.get('worst_severity') or 'none'})"
        for sh in sprint_health
    ]
    health_text = "\n".join(health_lines) or "  - No sprint data available"

    immediate_actions = [m for m in mitigations if m["urgency"] == "IMMEDIATE"]
    action_lines = "\n".join(
        f"  - [{m['flag']}] Ticket {m['ticket_id']}: {m['action'][:120]}..."
        for m in immediate_actions[:5]
    ) or "  - No immediate actions required"

    return f"""You are a Senior Technical Program Manager writing the daily async standup summary for {program_name} ({domain}).

Date: {today}

Sprint Health:
{health_text}

Risk Flags Detected ({len(risk_flags)} total):
{flag_summary}

Immediate Actions ({len(immediate_actions)}):
{action_lines}

Write a 3-paragraph standup summary in flowing prose — NO bullet points, NO markdown headers, NO lists.

Paragraph 1: Overall program health today — reference sprint badges and what they signal about delivery trajectory.
Paragraph 2: Key risks and blockers — what's threatening the sprint, what specifically is stalled or blocked and why it matters.
Paragraph 3: Actions and forward look — what needs to happen today and this week, who owns it, and the confidence level for sprint completion.

Tone: clear, direct, VP-readable. No fluff. Assume the reader is a busy senior leader who reads this in 45 seconds.
Return ONLY the three paragraphs. No labels, no preamble, no markdown."""


def _build_escalation_prompt(
    escalation_mitigations: List[dict],
    tickets: List[dict],
    ctx_dict: dict,
    run_id: str,
) -> str:
    program_name = ctx_dict.get("program_name", "the program")
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    ticket_by_id = {t["id"]: t for t in tickets}

    esc_details = []
    for m in escalation_mitigations[:10]:  # cap at 10 for prompt size
        t = ticket_by_id.get(m["ticket_id"], {})
        esc_details.append(
            f"  - {m['ticket_id']} ({m['flag']}, {m['severity']}): "
            f"'{t.get('title', 'Unknown')}' — assigned to {t.get('assignee', 'unassigned')}, "
            f"team {t.get('team', 'unknown')}. Action: {m['action'][:150]}"
        )
    details_text = "\n".join(esc_details)

    return f"""You are a Senior TPM writing a formal escalation memo for {program_name}.

Date: {today}
Run ID: {run_id[:8]}

ESCALATION ITEMS ({len(escalation_mitigations)} total requiring immediate VP attention):
{details_text}

Write a professional escalation memo in the style of an internal executive communication. Structure:
1. SUBJECT line (one sentence, no label needed — just write it as the opening)
2. SITUATION — 2-3 sentences: what is happening, how severe, why now
3. IMPACT — 2-3 sentences: what is at risk (milestones, delivery, team throughput)
4. RECOMMENDED ACTIONS — 3-5 numbered items, each under 2 sentences, owner named
5. DECISION NEEDED — one clear sentence about what you need from the VP/decision-maker

Tone: urgent but composed. Data-driven. No corporate filler phrases. No markdown bold or headers — use plain text.
Return ONLY the memo content. No labels, no preamble."""


def _build_risk_digest_prompt(
    risk_flags: List[dict],
    tickets: List[dict],
    ctx_dict: dict,
) -> str:
    program_name = ctx_dict.get("program_name", "the program")
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    ticket_by_id = {t["id"]: t for t in tickets}

    # Sort CRITICAL → LOW
    sorted_flags = sorted(
        risk_flags,
        key=lambda f: _SEVERITY_ORDER.index(f["severity"]) if f["severity"] in _SEVERITY_ORDER else 99
    )

    flag_lines = []
    for f in sorted_flags[:20]:  # cap for prompt size
        t = ticket_by_id.get(f["ticket_id"], {})
        flag_lines.append(
            f"  [{f['severity']}] {f['ticket_id']} ({f['flag']}): "
            f"'{t.get('title', 'Unknown')}' — {f.get('reason', '')[:100]}"
        )
    flags_text = "\n".join(flag_lines) or "  No risks detected."

    return f"""You are a Senior TPM writing a risk digest for {program_name}.

Date: {today}

RISKS DETECTED ({len(risk_flags)} total, sorted by severity):
{flags_text}

Write a risk digest: one concise line per risk, sorted CRITICAL first. Each line must contain:
  - The ticket ID
  - Risk type (STALE/BLOCKED/SCOPE_CREEP/OVERLOADED)
  - Severity in brackets
  - A plain-English summary of the risk in under 15 words

Format each line exactly as:
{program_name} · TICKET-ID · FLAG [SEVERITY] — plain English summary

Example:
Project Atlas · ATLAS-007 · BLOCKED [CRITICAL] — Payments API integration stalled; blocking 3 downstream tickets

Return ONLY the formatted lines, one per risk. No preamble, no totals, no markdown."""


def _summarize_flags(risk_flags: List[dict]) -> str:
    counts: dict[str, int] = {}
    for f in risk_flags:
        counts[f["flag"]] = counts.get(f["flag"], 0) + 1
    if not counts:
        return "  - No risks detected this cycle"
    return "\n".join(f"  - {flag}: {count}" for flag, count in counts.items())


# ── Claude API call ───────────────────────────────────────────────────────────

async def _call_claude(prompt: str, system_hint: str = "") -> Optional[str]:
    """
    Call Claude API. Returns the text response or None on failure.
    Never raises — all errors are logged and return None.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.info("ANTHROPIC_API_KEY not set — using programmatic fallback.")
        return None

    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=api_key)
        model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        max_tokens = int(os.getenv("CLAUDE_MAX_TOKENS", "4096"))

        system = system_hint or (
            "You are an expert Senior Technical Program Manager. "
            "Write clear, direct, executive-quality communications. "
            "Always return only the requested content — no preamble, no markdown labels."
        )

        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```[a-z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return raw

    except Exception:
        logger.exception("Claude API call failed in communication agent.")
        return None


# ── Programmatic fallbacks ────────────────────────────────────────────────────

def _fallback_standup(
    sprint_health: List[dict],
    risk_flags: List[dict],
    mitigations: List[dict],
    ctx_dict: dict,
) -> str:
    program_name = ctx_dict.get("program_name", "the program")
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    escalate_sprints = [sh for sh in sprint_health if sh["health_badge"] == "ESCALATE"]
    alert_sprints    = [sh for sh in sprint_health if sh["health_badge"] == "ALERT"]
    watch_sprints    = [sh for sh in sprint_health if sh["health_badge"] == "WATCH"]

    # Sprint health summary with names
    if escalate_sprints:
        sprint_names = ", ".join(s["name"].split(" — ")[0] for s in escalate_sprints[:2])
        health_summary = (
            f"{sprint_names} {'are' if len(escalate_sprints) > 1 else 'is'} in ESCALATE — "
            f"critical-severity risks are actively threatening delivery commitments"
        )
        confidence = "low"
    elif alert_sprints:
        sprint_names = ", ".join(s["name"].split(" — ")[0] for s in alert_sprints[:2])
        health_summary = (
            f"{sprint_names} {'are' if len(alert_sprints) > 1 else 'is'} in ALERT — "
            f"high-severity risks require immediate resolution to stay on track"
        )
        confidence = "moderate"
    elif watch_sprints:
        sprint_names = ", ".join(s["name"].split(" — ")[0] for s in watch_sprints[:2])
        health_summary = f"{sprint_names} {'are' if len(watch_sprints) > 1 else 'is'} under WATCH with low-to-medium risks"
        confidence = "good"
    else:
        health_summary = "all sprints are HEALTHY with no active risks detected"
        confidence = "high"

    critical_flags = [f for f in risk_flags if f["severity"] == "CRITICAL"]
    high_flags     = [f for f in risk_flags if f["severity"] == "HIGH"]
    blocked        = [f for f in risk_flags if f["flag"] == "BLOCKED"]
    stale          = [f for f in risk_flags if f["flag"] == "STALE"]

    # Name the top critical blocked tickets specifically
    top_blocked_ids = list({f["ticket_id"] for f in blocked if f["severity"] in ("CRITICAL", "HIGH")})[:3]
    top_stale_ids   = list({f["ticket_id"] for f in stale if f["severity"] in ("CRITICAL", "HIGH")})[:2]

    risk_parts = []
    if top_blocked_ids:
        risk_parts.append(
            f"{', '.join(top_blocked_ids)} {'are' if len(top_blocked_ids) > 1 else 'is'} "
            f"blocked with unresolved upstream dependencies"
        )
    if top_stale_ids:
        risk_parts.append(
            f"{', '.join(top_stale_ids)} {'have' if len(top_stale_ids) > 1 else 'has'} "
            f"had no progress and crossed the stale threshold"
        )
    total_blocked = len(set(f["ticket_id"] for f in blocked))
    total_stale   = len(set(f["ticket_id"] for f in stale))
    if total_blocked > len(top_blocked_ids):
        risk_parts.append(f"{total_blocked} ticket(s) total are blocked this cycle")
    if total_stale > len(top_stale_ids):
        risk_parts.append(f"{total_stale} ticket(s) total are stale")
    if not risk_parts:
        if risk_flags:
            risk_parts.append(f"{len(risk_flags)} risk flag(s) detected — no critical or high blockers")
        else:
            risk_parts.append("no significant risks are active this cycle")

    # Action owners
    immediate = [m for m in mitigations if m["urgency"] == "IMMEDIATE"]
    this_sprint = [m for m in mitigations if m["urgency"] == "THIS_SPRINT"]
    immediate_owners = sorted({m["suggested_owner"] for m in immediate if m["suggested_owner"]})

    action_parts = []
    if immediate:
        owner_str = ", ".join(immediate_owners[:3]) or "TBD"
        action_parts.append(
            f"{len(immediate)} item(s) need resolution today — "
            f"{owner_str} {'are' if len(immediate_owners) > 1 else 'is'} the named owner(s)"
        )
    if this_sprint:
        action_parts.append(f"{len(this_sprint)} item(s) are tracked for this-sprint resolution")
    if not action_parts:
        action_parts.append("no actions are pending beyond routine monitoring")

    has_escalations = any(m.get("requires_escalation") for m in mitigations)
    milestone_risks = list({
        ctx_dict.get("critical_path", [{}])[0].get("name", "")
        for m in mitigations if m.get("requires_escalation")
    } - {""})

    p1 = (
        f"{program_name} standup — {today}. "
        f"{health_summary}. "
        f"Delivery confidence is {confidence} across {len(sprint_health)} active sprint(s) — "
        f"{'immediate executive attention is required.' if confidence == 'low' else 'the team has a path forward if blockers are resolved this week.'}"
    )

    p2 = (
        f"Key risks this cycle: {'; '.join(risk_parts)}. "
        f"{len(risk_flags)} total flag(s) across {len({f['ticket_id'] for f in risk_flags})} ticket(s). "
        f"{'These are actively threatening sprint completion and milestone commitments — blast radius is expanding with each day of inaction.' if (critical_flags or high_flags) else 'These are being monitored and are manageable within the current sprint.'}"
    )

    p3 = (
        f"For today: {'; '.join(action_parts)}. "
        f"{'Escalation memos have been raised for VP attention' + (f' — {milestone_risks[0]} milestone is in scope' if milestone_risks else '') + '.' if has_escalations else 'No escalations are required at this time.'} "
        f"Next automated cycle will run in approximately 30 seconds to confirm resolution progress."
    )

    return f"{p1}\n\n{p2}\n\n{p3}"


def _fallback_escalation_memo(
    escalation_mitigations: List[dict],
    tickets: List[dict],
    ctx_dict: dict,
    run_id: str,
) -> str:
    program_name = ctx_dict.get("program_name", "the program")
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    ticket_by_id = {t["id"]: t for t in tickets}

    critical = [m for m in escalation_mitigations if m["severity"] == "CRITICAL"]
    high = [m for m in escalation_mitigations if m["severity"] == "HIGH"]

    subject = (
        f"ESCALATION REQUIRED — {len(escalation_mitigations)} critical/high risk(s) "
        f"threatening {program_name} delivery ({today})"
    )

    situation = (
        f"The autonomous TPM system has detected {len(escalation_mitigations)} risk(s) "
        f"requiring immediate executive attention in {program_name} "
        f"({len(critical)} CRITICAL, {len(high)} HIGH). "
        f"These items have been flagged because they meet escalation criteria: "
        f"severity at HIGH or CRITICAL with flag types BLOCKED or STALE. "
        f"Without intervention today, delivery commitments are at risk."
    )

    affected_milestones = set()
    for m in escalation_mitigations:
        t = ticket_by_id.get(m["ticket_id"], {})
        mt = t.get("milestone_target")
        if mt:
            affected_milestones.add(mt)

    if affected_milestones:
        milestone_text = f"Milestones at risk: {', '.join(sorted(affected_milestones))}."
    else:
        milestone_text = "Sprint completion targets are at risk if these items are not resolved within 24 hours."

    impact = (
        f"The affected tickets span {len({ticket_by_id.get(m['ticket_id'], {}).get('team', 'unknown') for m in escalation_mitigations})} team(s). "
        f"{milestone_text} "
        f"Continued inaction will compound — each day of delay increases the probability "
        f"of sprint spillover and downstream milestone slippage."
    )

    # Build actions — use a clean sentence-by-sentence format, no mid-sentence truncation
    actions = []
    # Deduplicate by ticket_id (a ticket can have multiple flags)
    seen_tickets: set[str] = set()
    action_index = 1
    for m in escalation_mitigations:
        if action_index > 5:
            break
        tid = m["ticket_id"]
        t = ticket_by_id.get(tid, {})
        owner = m.get("suggested_owner") or t.get("assignee") or "TBD"
        title = t.get("title", "Unknown ticket")
        assignee = t.get("assignee", "the current assignee")
        team = t.get("team", "the owning team")

        if m["flag"] == "BLOCKED":
            blocker_ids = t.get("blocker_ids") or []
            blocker_str = ", ".join(blocker_ids) if blocker_ids else "upstream dependency"
            action_text = (
                f'{tid} ("{title[:50]}") is BLOCKED by {blocker_str}. '
                f'{assignee} ({team}) must coordinate unblock. '
                f'Escalate to {owner} if not cleared by end of day.'
            )
        elif m["flag"] == "STALE":
            stale_days = m["action"].split("for ")[1].split(" day")[0] if "for " in m["action"] else "multiple days"
            action_text = (
                f'{tid} ("{title[:50]}") has had no progress for {stale_days} day(s). '
                f'Schedule immediate sync with {assignee} ({team}) to identify blockers or reassign. '
                f'Owner: {owner}.'
            )
        else:
            action_text = f"{tid}: {m['action'][:200]}"

        if tid not in seen_tickets or m["severity"] == "CRITICAL":
            actions.append(f"{action_index}. [{m['flag']} / {m['severity']}] {action_text}")
            seen_tickets.add(tid)
            action_index += 1

    actions_text = "\n".join(actions)

    decision = (
        f"Decision needed: confirm ownership and authorize resource re-assignment "
        f"for the {len(critical)} CRITICAL item(s) above. "
        f"Response required by end of business today to avoid milestone slippage."
    )

    return (
        f"{subject}\n\n"
        f"SITUATION\n{situation}\n\n"
        f"IMPACT\n{impact}\n\n"
        f"RECOMMENDED ACTIONS\n{actions_text}\n\n"
        f"DECISION NEEDED\n{decision}\n\n"
        f"[Auto-generated by {program_name} TPM Intelligence System | Run {run_id[:8]}]"
    )


def _fallback_risk_digest(
    risk_flags: List[dict],
    tickets: List[dict],
    ctx_dict: dict,
) -> str:
    program_name = ctx_dict.get("program_name", "the program")
    ticket_by_id = {t["id"]: t for t in tickets}

    sorted_flags = sorted(
        risk_flags,
        key=lambda f: _SEVERITY_ORDER.index(f["severity"]) if f["severity"] in _SEVERITY_ORDER else 99
    )

    lines = []
    for f in sorted_flags:
        t = ticket_by_id.get(f["ticket_id"], {})
        title = t.get("title", "Unknown ticket")
        # Truncate title to 8 words for digest brevity
        words = title.split()
        short_title = " ".join(words[:8]) + ("..." if len(words) > 8 else "")
        lines.append(
            f"{program_name} · {f['ticket_id']} · {f['flag']} [{f['severity']}] — {short_title}"
        )

    return "\n".join(lines) if lines else f"{program_name} · No risks detected this cycle."


# ── Redis updates ─────────────────────────────────────────────────────────────

async def _update_redis_cache(
    program_id_str: str,
    tickets: List[dict],
    sprint_health: List[dict],
    executive_outputs: dict,
    run_id: str,
    cycle_number: int,
) -> None:
    """Update Redis cache keys and publish pipeline completion event."""
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r: aioredis.Redis | None = None
    try:
        r = aioredis.from_url(redis_url)

        # Update tickets cache
        await r.set(
            f"tickets:current:{program_id_str}",
            json.dumps(tickets, default=str),
            ex=35,
        )

        # Update sprint health cache
        await r.set(
            f"sprint_health:current:{program_id_str}",
            json.dumps(sprint_health, default=str),
            ex=35,
        )

        # Publish pipeline completion to stream
        await r.xadd(
            f"pipeline:{program_id_str}",
            {
                "event": "cycle_complete",
                "run_id": run_id,
                "cycle_number": str(cycle_number),
                "output_types": ",".join(executive_outputs.keys()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            maxlen=200,
            approximate=True,
        )

        logger.info(
            "Communication [run=%s]: Redis cache updated; pipeline event published.",
            run_id[:8],
        )
    except Exception:
        logger.exception("Redis update failed in communication agent — non-fatal.")
    finally:
        if r is not None:
            await r.aclose()


# ── Agent node ────────────────────────────────────────────────────────────────

async def run_communication_agent(state: PipelineState) -> dict:
    """
    LangGraph node: Communication Agent.

    Generates STANDUP_SUMMARY, ESCALATION_MEMO (if escalations exist),
    RISK_DIGEST. Persists to executive_outputs table. Updates Redis cache.
    """
    risk_flags = state.get("risk_flags", [])
    mitigations = state.get("mitigations", [])
    tickets = state.get("tickets", [])
    sprint_health = state.get("sprint_health", [])
    run_id = state["run_id"]
    cycle_number = state["cycle_number"]
    program_id_str = state["program_id"]
    ctx_dict = state["program_context"]
    program_uuid = uuid.UUID(program_id_str)
    domain = ctx_dict.get("domain", "unknown")

    executive_outputs: dict[str, str] = {}

    # ── 1. Standup Summary ────────────────────────────────────────────────
    standup_prompt = _build_standup_prompt(
        sprint_health, risk_flags, mitigations, tickets, ctx_dict
    )
    standup_text = await _call_claude(standup_prompt)
    if standup_text is None:
        standup_text = _fallback_standup(sprint_health, risk_flags, mitigations, ctx_dict)
    executive_outputs["STANDUP_SUMMARY"] = standup_text

    # ── 2. Escalation Memo (only if escalations exist) ────────────────────
    escalation_mitigations = [m for m in mitigations if m.get("requires_escalation")]
    if escalation_mitigations:
        esc_prompt = _build_escalation_prompt(
            escalation_mitigations, tickets, ctx_dict, run_id
        )
        esc_text = await _call_claude(esc_prompt)
        if esc_text is None:
            esc_text = _fallback_escalation_memo(
                escalation_mitigations, tickets, ctx_dict, run_id
            )
        executive_outputs["ESCALATION_MEMO"] = esc_text

    # ── 3. Risk Digest ────────────────────────────────────────────────────
    if risk_flags:
        digest_prompt = _build_risk_digest_prompt(risk_flags, tickets, ctx_dict)
        digest_text = await _call_claude(digest_prompt)
        if digest_text is None:
            digest_text = _fallback_risk_digest(risk_flags, tickets, ctx_dict)
    else:
        digest_text = _fallback_risk_digest([], tickets, ctx_dict)
    executive_outputs["RISK_DIGEST"] = digest_text

    # ── Persist to executive_outputs table ────────────────────────────────
    async with AsyncSessionLocal() as session:
        for output_type, content in executive_outputs.items():
            session.add(
                ExecutiveOutput(
                    program_id=program_uuid,
                    domain=domain,
                    run_id=run_id,
                    cycle_number=cycle_number,
                    output_type=output_type,
                    content=content,
                )
            )

        # Agent decision record
        output_types_str = ", ".join(executive_outputs.keys())
        decision_text = (
            f"Generated {len(executive_outputs)} executive output(s): {output_types_str}. "
            f"{'Claude API used.' if os.getenv('ANTHROPIC_API_KEY', '').strip() else 'Programmatic fallback used (no API key).'}"
        )
        reasoning_text = (
            f"STANDUP_SUMMARY: {len(standup_text)} chars. "
            + (f"ESCALATION_MEMO: {len(executive_outputs.get('ESCALATION_MEMO', ''))} chars. "
               if "ESCALATION_MEMO" in executive_outputs else "ESCALATION_MEMO: skipped (no escalations). ")
            + f"RISK_DIGEST: {len(digest_text)} chars."
        )

        session.add(
            AgentDecision(
                program_id=program_uuid,
                domain=domain,
                run_id=run_id,
                cycle_number=cycle_number,
                agent_name="communication",
                decision=decision_text,
                reasoning=reasoning_text,
                input_summary={
                    "program_id": program_id_str,
                    "cycle_number": cycle_number,
                    "risk_flag_count": len(risk_flags),
                    "escalation_count": len(escalation_mitigations),
                },
                output_summary={
                    "output_types": list(executive_outputs.keys()),
                    "standup_chars": len(standup_text),
                    "escalation_memo_generated": "ESCALATION_MEMO" in executive_outputs,
                    "risk_digest_chars": len(digest_text),
                },
            )
        )
        await session.commit()

    decision_dict = {
        "agent": "communication",
        "decision": decision_text,
        "reasoning": reasoning_text,
        "input_summary": {
            "program_id": program_id_str,
            "cycle_number": cycle_number,
            "risk_flag_count": len(risk_flags),
            "escalation_count": len(escalation_mitigations),
        },
        "output_summary": {
            "output_types": list(executive_outputs.keys()),
            "standup_chars": len(standup_text),
            "escalation_memo_generated": "ESCALATION_MEMO" in executive_outputs,
        },
    }

    # ── Update Redis cache ────────────────────────────────────────────────
    await _update_redis_cache(
        program_id_str, tickets, sprint_health, executive_outputs, run_id, cycle_number
    )

    logger.info(
        "Communication [run=%s cycle=%d]: %d output(s) generated: %s",
        run_id[:8],
        cycle_number,
        len(executive_outputs),
        output_types_str,
    )

    existing_decisions = list(state.get("agent_decisions", []))
    return {
        "executive_outputs": executive_outputs,
        "agent_decisions": existing_decisions + [decision_dict],
    }
