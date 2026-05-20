# Document 1 — Product Specification
## Autonomous TPM Intelligence System (ATIS)

---

### Product Name and Description

**Autonomous TPM Intelligence System (ATIS)**
A multi-agent platform that monitors a simulated software program backlog, detects TPM-class problems every 30 seconds without human prompting, and generates the written outputs a senior TPM would normally produce by hand.

---

### The Problem It Solves

Senior TPMs spend a disproportionate amount of their cycle on work that is pattern-recognizable and repeatable: scanning backlogs for stalled tickets, tracing blocked dependency chains, identifying overloaded assignees, writing standup summaries, drafting escalation memos, and producing risk digests. None of this requires judgment unique to a human — it requires consistent application of rules against structured data, plus coherent written synthesis.

The cost of doing this manually is latency. A risk that emerges Wednesday afternoon may not surface until Friday's standup. An overloaded assignee may not be visible until a milestone slips. A blocked critical-path ticket may not trigger escalation until a VP asks.

ATIS closes that gap. It runs the detection and writing loop continuously, every 30 seconds, with full reasoning traces so every decision is auditable.

---

### Who It Is For

**Primary user:** Senior TPM or program lead who wants situational awareness without manual backlog triage.

**Secondary user:** Engineering manager or tech lead who attends standup and needs accurate, current risk status without preparing it themselves.

**Audience for executive outputs:** Program lead, VP Engineering, or equivalent — the Escalation Memo is written for this reader.

---

### What It Does — Functional Requirements

1. Simulates a Jira-like backlog for a named program, with tickets that mutate state over time to produce realistic risk scenarios.
2. Runs a 5-agent pipeline every 30 seconds autonomously. No human trigger required.
3. Detects four ticket-level risk types: `STALE`, `BLOCKED`, `SCOPE_CREEP`, `OVERLOADED`. Each flag carries a severity: `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL`, determined by configurable rule thresholds.
4. Computes a sprint-level health badge — `HEALTHY`, `WATCH`, `ALERT`, or `ESCALATE` — derived from the worst active risk severity in that sprint. Set entirely by the Risk Detection Agent.
5. Maps dependency chains and identifies cascading block risks across teams. If a blocked ticket blocks other tickets, the full chain is traced and included in the risk output.
6. Generates recommendations for each active risk — named action, suggested owner, urgency — via the Mitigation Agent.
7. Produces three executive documents per cycle via the Communication Agent using Claude API: Standup Summary, Escalation Memo (only when HIGH or CRITICAL risk exists), Risk Digest.
8. Logs every agent decision with full reasoning trace to PostgreSQL. The dashboard reads from this log — it computes nothing.
9. Streams live updates to the dashboard via WebSocket on every cycle completion.
10. Supports domain-extensible configuration via the Program Context Layer.

---

### What It Does Not Do in v1 — Explicit Scope Boundaries

- No integration with real Jira, Linear, GitHub Projects, or any external project management tool.
- No email or Slack delivery of executive outputs.
- No user authentication or access control.
- No machine learning models. All risk detection is rule-based.
- No Kafka. Redis Streams is the only message streaming mechanism.
- No Next.js. Frontend is React + Vite only.
- No multi-user collaboration or concurrent session management.
- No historical trend analysis across multiple program cycles (data is stored, but no trend visualization in v1).
- No mobile-responsive design.
- No ability to edit tickets or program configuration from the UI.

---

### The Five Agent Responsibilities and Their Outputs

**Agent 1 — Telemetry Agent**
Responsibility: Pull the current backlog state from PostgreSQL for the active program. Normalize all ticket data. Compute sprint-level aggregates: total points, completed points, completion percentage, ticket counts by status. Detect `stale_since` values by comparing `updated_at` against the configured staleness threshold.
Output: Normalized ticket list with staleness computed. Sprint summaries with completion percentages. Written to LangGraph state as `tickets` and `sprint_summaries`. Agent decision logged: "Loaded N tickets across M sprints. X tickets flagged as potentially stale."

**Agent 2 — Risk Detection Agent**
Responsibility: Apply four rule sets to every ticket. Assign `risk_flag` and `risk_severity` where rules fire. Determine sprint health badge per sprint based on worst severity active in that sprint. Rules are threshold-driven and sourced from Program Context configuration.
Risk rules:
- `STALE`: ticket in `TODO` or `IN_PROGRESS`, not updated for longer than `stale_ticket_days` threshold. Severity scales with staleness duration and whether ticket is on critical path.
- `BLOCKED`: ticket has one or more `blocker_ids` whose status is not `DONE`. Severity scales with priority and critical path status.
- `SCOPE_CREEP`: `scope_changed = true` or story points increased beyond `scope_creep_story_point_increase` threshold. Severity scales with magnitude of increase.
- `OVERLOADED`: assignee has more than `overload_points_per_assignee` story points of active `IN_PROGRESS` work across all tickets. Flag applied to each affected ticket belonging to that assignee.

Sprint health badge assignment:
- `HEALTHY` = no active flags
- `WATCH` = any `LOW` or `MEDIUM` flags
- `ALERT` = any `HIGH` flags
- `ESCALATE` = any `CRITICAL` flags

Output: Updated ticket list with `risk_flag` and `risk_severity` set. Sprint health badges. Written to LangGraph state as `risk_flags` list and `sprint_health` list. Agent decision logged per risk flag applied, with rule that fired and values that triggered it.

**Agent 3 — Dependency Analysis Agent**
Responsibility: Traverse the `blocker_ids` graph for all `BLOCKED` tickets. Identify transitive blocking chains (A blocks B blocks C). Compute blast radius: how many tickets are downstream of each blocker. Elevate severity of upstream blockers when chain depth exceeds one. Add dependency chain context to existing `BLOCKED` risk flags.
Output: Enriched risk flags with `dependency_chain` field showing full chain and blast radius. New risk flags created for previously unflagged upstream tickets where chain depth warrants it. Written to LangGraph state as updated `risk_flags`. Agent decision logged: chain identified, depth, blast radius, any severity elevations.

**Agent 4 — Mitigation Agent**
Responsibility: For each active risk flag, generate a structured recommendation. Recommendation includes: action description, suggested owner (derived from assignee and team fields), urgency classification, and whether it requires escalation. Does not call Claude API in v1. All output is rule-derived from risk type, severity, and ticket metadata. This is a deliberate design decision — rule-based mitigation keeps the agent fast, deterministic, and auditable. Claude-powered mitigation reasoning is scoped to v2.
Output: `mitigations` list — one entry per risk flag, containing `risk_flag_id`, `action`, `suggested_owner`, `urgency` (`IMMEDIATE`, `THIS_SPRINT`, `NEXT_SPRINT`), `requires_escalation` (bool). Written to LangGraph state. Agent decision logged per mitigation with reasoning.

**Agent 5 — Communication Agent**
Responsibility: Synthesize all agent outputs into three written documents using Claude API. Constructs a structured prompt for each document type, incorporating sprint health, risk flags, dependency chains, mitigations, and program context. Persists outputs to PostgreSQL. Publishes final state to Redis for WebSocket broadcast.
Output: Three documents (detailed below). Written to `executive_outputs` table and LangGraph state. Agent decision logged: "Generated standup summary, risk digest. Escalation memo [generated / skipped — no HIGH/CRITICAL risks this cycle]."

---

### The Four Dashboard Panels

**Panel 1 — Program Health Bar**
Displays top-level health status per sprint. One row per sprint. Each row shows: sprint name, completion percentage as a numeric and progress bar, and health badge (`HEALTHY` / `WATCH` / `ALERT` / `ESCALATE`) color-coded by severity. Badge and percentage are set entirely by the Risk Detection Agent output — the dashboard renders them, does not compute them. Source: `sprint_health` from latest agent cycle.

**Panel 2 — Backlog View**
Displays the live ticket list. Each row shows: ticket ID, title, assignee, team, status, priority, sprint, and inline risk flag (`STALE` / `BLOCKED` / `SCOPE_CREEP` / `OVERLOADED`) if one exists. Risk flags are rendered as colored badges. Filterable by team and by risk severity. Panel reads from the latest agent-annotated ticket state — no filtering logic computes risk, it only filters on what agents already set. Source: `tickets` with `risk_flag` and `risk_severity` populated by agents.

**Panel 3 — Agent Activity Stream**
Real-time chronological feed of every decision every agent made during the current cycle. Each entry shows: agent name, timestamp, decision summary (one line), and reasoning (collapsed by default, expandable on click). Entries arrive via WebSocket as the cycle progresses. Source: `agent_decisions` table, streamed live.

**Panel 4 — Executive Output Panel**
Displays the three documents the Communication Agent produced. One tab or section per document type: Standup Summary, Escalation Memo, Risk Digest. Shows the most recent version of each with generation timestamp. Escalation Memo section shows "No escalation required this cycle" when the agent did not produce one. Source: `executive_outputs` table, latest entry per output type.

---

### The Three Executive Outputs

**1. Standup Summary**
Type: 3-paragraph narrative prose
Audience: TPM and program team, read at daily standup
Trigger: Every cycle
Contents:
- Paragraph 1: Overall program health this cycle — which sprints are healthy, which are flagged, aggregate completion percentage
- Paragraph 2: Key risks detected — what types, which teams affected, any dependency chain impacts
- Paragraph 3: Recommended actions with named owners and urgency level

Tone: Direct, factual, senior TPM voice. No hedging. No filler. Written as if delivered verbally at standup.

**2. Escalation Memo**
Type: Concise formal memo, maximum one page
Audience: Program lead or VP Engineering
Trigger: Only when Risk Detection Agent classifies at least one risk as `HIGH` or `CRITICAL`
Contents: What happened (the risk, ticket, team). When detected (cycle timestamp). Why it matters (business or milestone impact, derived from priority and critical path status). What needs to happen now (specific action). Who needs to act (named owner and role).
Tone: Crisp, executive-ready. No background context, no filler. Written for a reader who has 90 seconds.

**3. Risk Digest**
Type: Structured bullet list
Audience: TPM and tech leads
Trigger: Every cycle
Contents: All active risks ranked by severity — `ESCALATE`-level first, then `ALERT`, then `WATCH`. Each entry: risk type, affected ticket ID and title, affected team, severity level, one-line recommendation. Header line: total active risk count and cycle timestamp.
Tone: Scannable. Consistent format per entry. No prose.

---

### How the Program Context Layer Makes the System Reusable

The Program Context Layer is a YAML configuration file per program, loaded at startup. It defines all thresholds, rules, and structural parameters that vary across program domains. No code changes are required to run ATIS against a different program or domain — only the configuration file changes.

What the Program Context defines:
- **Domain**: string identifier (`enterprise_software`, `government_compliance`, etc.) stored on all database records for queryability
- **Risk thresholds**: staleness window in days, overload story point ceiling, scope creep percentage trigger — these differ by domain (a compliance program may treat 3-day staleness as critical; a product sprint may treat 7 days as medium)
- **Sprint health rules**: what percentage of flagged tickets in a sprint triggers each health badge level
- **Escalation rules**: which risk flag types and severities trigger an Escalation Memo
- **Team names**: used for Backlog View filtering and for Communication Agent output attribution
- **Critical path weight**: multiplier applied to severity scoring for tickets where `is_on_critical_path = true`

Adding a new program requires: create a new YAML config file, create a new program record in PostgreSQL. No agent code changes. No schema changes.

---

### Success Criteria — How We Know v1 Is Working Correctly

1. **Autonomous operation**: Agent loop runs for 10+ consecutive 30-second cycles without error or manual intervention.
2. **Risk coverage**: All four risk flag types (`STALE`, `BLOCKED`, `SCOPE_CREEP`, `OVERLOADED`) appear in agent output within the first 5 simulation cycles.
3. **Severity correctness**: A ticket on the critical path with a `BLOCKED` flag receives `HIGH` or `CRITICAL` severity. A P3 non-critical-path stale ticket receives `LOW` or `MEDIUM`.
4. **Sprint health derivation**: A sprint containing a `CRITICAL` risk receives `ESCALATE` badge. A sprint with only `LOW` risks receives `WATCH` or `HEALTHY`.
5. **Executive output generation**: Standup Summary and Risk Digest are generated every cycle. Escalation Memo is generated in cycles where HIGH/CRITICAL risks exist and suppressed in cycles where they do not.
6. **Escalation Memo trigger integrity**: Zero Escalation Memos exist in the database for cycles where no `HIGH` or `CRITICAL` risk was active.
7. **Reasoning traceability**: Every risk flag in the database has a corresponding `agent_decisions` entry with a non-empty `reasoning` field identifying which rule fired and what values triggered it.
8. **Dashboard live update**: Dashboard reflects new agent cycle output within 2 seconds of cycle completion.
9. **Domain extensibility**: Swapping Program Context configuration for the same ticket dataset produces different severity outputs and health badges without any code changes.
10. **Decision log completeness**: `agent_decisions` table contains at least one entry per agent per cycle, with no gaps.
