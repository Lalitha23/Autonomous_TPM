# Document 2 — Technical Architecture
## Autonomous TPM Intelligence System (ATIS)

---

### Full System Architecture — Layers and Responsibilities

```
┌─────────────────────────────────────────────────────────────┐
│                        FRONTEND                             │
│         React + Vite │ Tailwind │ Recharts │ D3             │
│  4 panels — reads from API + WebSocket, computes nothing    │
└───────────────────────────┬─────────────────────────────────┘
                            │ REST + WebSocket
┌───────────────────────────▼─────────────────────────────────┐
│                       FASTAPI BACKEND                       │
│         API routes │ WebSocket manager │ CORS               │
└──────┬──────────────────────┬──────────────────────────┬────┘
       │                      │                          │
┌──────▼──────┐    ┌──────────▼──────────┐   ┌──────────▼──────┐
│  POSTGRESQL │    │   AGENT PIPELINE    │   │      REDIS      │
│ SQLAlchemy  │    │  LangGraph + Claude │   │ Cache │ Streams │
│  + Alembic  │    │  5-agent graph      │   │ Pub/Sub         │
└─────────────┘    └──────────┬──────────┘   └─────────────────┘
                              │
                   ┌──────────▼──────────┐
                   │  SIMULATION ENGINE  │
                   │  Ticket mutations   │
                   │  Event publishing   │
                   └─────────────────────┘
```

**Layer responsibilities:**
- **Frontend**: Pure display layer. Receives data from REST API on load, then live updates via WebSocket. No business logic, no risk computation.
- **FastAPI Backend**: Serves API routes, manages WebSocket connections, owns the agent loop scheduler. Does not compute risk — serves what agents stored.
- **Agent Pipeline**: LangGraph-orchestrated 5-agent graph. Runs on 30-second cadence. All intelligence lives here.
- **PostgreSQL**: Source of truth for all persisted state — tickets, decisions, outputs, program config.
- **Redis**: Short-lived cache for current backlog state, pub/sub fan-out for WebSocket broadcasts, ordered event stream for pipeline events.
- **Simulation Engine**: Mutates ticket state before each agent cycle to produce realistic backlog evolution.

---

### The 5-Agent Pipeline

```
Simulation Engine
       │
       │ mutates tickets in DB
       ▼
┌──────────────┐     ┌─────────────────┐     ┌────────────────────┐
│  Telemetry   │────▶│ Risk Detection  │────▶│ Dependency         │
│  Agent       │     │ Agent           │     │ Analysis Agent     │
└──────────────┘     └─────────────────┘     └────────────────────┘
                                                        │
                              ┌─────────────────────────┘
                              ▼
                     ┌─────────────────┐     ┌─────────────────────┐
                     │   Mitigation    │────▶│  Communication      │
                     │   Agent         │     │  Agent              │
                     └─────────────────┘     └─────────────────────┘
                                                        │
                                             persists to DB + Redis
```

---

### Agent Inputs and Outputs

**Telemetry Agent**
Input from state: `program_id`, `program_context`, `cycle_number`, `run_id`
Reads from: PostgreSQL `tickets` table, `sprints` table for active program
Computes: sprint completion percentage = `sum(points_completed) / sum(story_points)` per sprint. Reads `stale_since` from the tickets table (written by Simulation Engine) to identify stale tickets. Telemetry Agent never writes to the database.
Output to state:
```
tickets: List[TicketRecord]           # full normalized ticket objects
sprint_summaries: List[SprintSummary] # {sprint_id, name, total_points,
                                       #  completed_points, pct_complete,
                                       #  ticket_count}
agent_decisions: [...appended with Telemetry entry]
```

**Risk Detection Agent**
Input from state: `tickets`, `sprint_summaries`, `program_context`
Evaluates: four rule sets against each ticket and each assignee aggregate
Output to state:
```
tickets: List[TicketRecord]           # same list, risk_flag + risk_severity populated
risk_flags: List[RiskFlag]            # {id, ticket_id, flag_type, severity,
                                       #  rule_fired, values_that_triggered,
                                       #  dependency_chain: null}
sprint_health: List[SprintHealth]     # {sprint_id, name, pct_complete,
                                       #  health_badge, worst_severity}
agent_decisions: [...appended]
```

**Dependency Analysis Agent**
Input from state: `tickets`, `risk_flags`
Traverses: `blocker_ids` graph on all BLOCKED tickets; builds adjacency map; performs BFS from each blocked ticket to find full chain
Output to state:
```
risk_flags: List[RiskFlag]            # BLOCKED flags enriched with:
                                       #  dependency_chain: {
                                       #    chain: List[ticket_id],
                                       #    depth: int,
                                       #    blast_radius: int
                                       #  }
                                       # New flags added for upstream blockers
                                       # where depth > 1 and chain warrants it
agent_decisions: [...appended]
```

**Mitigation Agent**
Input from state: `risk_flags`, `tickets`, `sprint_health`, `program_context`
Produces: one mitigation record per risk flag, derived from rule map (no Claude API call)
Rule map:
- STALE + CRITICAL → "Unblock and reassign immediately" | IMMEDIATE
- BLOCKED + HIGH/CRITICAL → "Resolve blocker [id] or escalate to [team lead]" | IMMEDIATE
- BLOCKED + MEDIUM → "Schedule blocker resolution this sprint" | THIS_SPRINT
- SCOPE_CREEP + HIGH → "Scope review required with [assignee] and PM" | THIS_SPRINT
- OVERLOADED + HIGH → "Redistribute [N] points from [assignee]" | IMMEDIATE

Output to state:
```
mitigations: List[Mitigation]         # {risk_flag_id, ticket_id, action,
                                       #  suggested_owner, urgency,
                                       #  requires_escalation: bool}
agent_decisions: [...appended]
```

**Communication Agent**
Input from state: `risk_flags`, `mitigations`, `sprint_health`, `tickets`, `program_context`, `run_id`, `cycle_number`
Calls: Claude API (`claude-sonnet-4-6`) three times (Standup Summary always, Risk Digest always, Escalation Memo only if `requires_escalation = true` on any mitigation)
Persists: all three outputs to PostgreSQL `executive_outputs` table; sprint health records to `sprints` table (`health_badge`, `worst_severity`, `last_run_id` per sprint)
Publishes: full pipeline result to Redis Stream and Redis pub/sub channel
Output to state:
```
executive_outputs: ExecutiveOutputs   # {standup_summary: str,
                                       #  escalation_memo: str | null,
                                       #  risk_digest: str}
agent_decisions: [...appended]
```

---

### LangGraph State Object

```python
class PipelineState(TypedDict):
    # Cycle identity
    run_id: str                          # UUID, unique per 30s cycle
    cycle_number: int                    # monotonically increasing
    started_at: str                      # ISO datetime string

    # Program config
    program_id: str
    program_context: ProgramContext      # full config loaded from YAML

    # Agent outputs — accumulated through pipeline
    tickets: List[TicketRecord]          # populated by Telemetry, annotated by Risk Detection
    sprint_summaries: List[SprintSummary]
    risk_flags: List[RiskFlag]           # populated by Risk Detection, enriched by Dependency Analysis
    sprint_health: List[SprintHealth]    # populated by Risk Detection
    mitigations: List[Mitigation]        # populated by Mitigation Agent
    executive_outputs: ExecutiveOutputs  # populated by Communication Agent

    # Cross-cutting
    agent_decisions: List[AgentDecision] # each agent appends its own entries
    errors: List[AgentError]             # any agent can append; pipeline continues on non-fatal errors
```

---

### Inter-Agent Message Contracts

Each agent receives the full state object and returns an updated copy. The contracts below define which fields each agent reads and which it writes.

| Agent | Reads | Writes |
|---|---|---|
| Telemetry | `program_id`, `program_context`, `run_id` | `tickets`, `sprint_summaries`, `agent_decisions` |
| Risk Detection | `tickets`, `sprint_summaries`, `program_context` | `tickets` (annotated), `risk_flags`, `sprint_health`, `agent_decisions` |
| Dependency Analysis | `tickets`, `risk_flags` | `risk_flags` (enriched), `agent_decisions` |
| Mitigation | `risk_flags`, `tickets`, `sprint_health`, `program_context` | `mitigations`, `agent_decisions` |
| Communication | `risk_flags`, `mitigations`, `sprint_health`, `tickets`, `program_context`, `run_id`, `cycle_number` | `executive_outputs`, `agent_decisions` |

No agent writes to a field that a prior agent in the chain will subsequently read — flow is strictly left to right.

---

### Data Layer — PostgreSQL Table Definitions

**Table: `programs`**
```
id                UUID          PRIMARY KEY DEFAULT gen_random_uuid()
name              VARCHAR(255)  NOT NULL
domain            VARCHAR(100)  NOT NULL      -- e.g. 'enterprise_software'
context_config    JSONB         NOT NULL      -- full ProgramContext as JSON
is_active         BOOLEAN       DEFAULT true
created_at        TIMESTAMPTZ   DEFAULT NOW()
updated_at        TIMESTAMPTZ   DEFAULT NOW()
```

**Table: `sprints`**
```
id                VARCHAR(100)  PRIMARY KEY   -- matches sprint_id on tickets
program_id        UUID          NOT NULL REFERENCES programs(id)
name              VARCHAR(255)  NOT NULL
start_date        DATE
end_date          DATE
health_badge      VARCHAR(10)   NULL          -- HEALTHY|WATCH|ALERT|ESCALATE
worst_severity    VARCHAR(10)   NULL          -- LOW|MEDIUM|HIGH|CRITICAL
last_run_id       VARCHAR(100)  NULL          -- ties health to cycle that set it
created_at        TIMESTAMPTZ   DEFAULT NOW()
updated_at        TIMESTAMPTZ   DEFAULT NOW()
```

**Table: `tickets`**
```
id                VARCHAR(100)  PRIMARY KEY
program_id        UUID          NOT NULL REFERENCES programs(id)
title             VARCHAR(500)  NOT NULL
description       TEXT
status            VARCHAR(20)   NOT NULL      -- TODO|IN_PROGRESS|IN_REVIEW|BLOCKED|DONE
priority          VARCHAR(5)    NOT NULL      -- P0|P1|P2|P3
assignee          VARCHAR(255)
team              VARCHAR(255)
sprint_id         VARCHAR(100)  REFERENCES sprints(id)
story_points      INTEGER       DEFAULT 0
points_completed  INTEGER       DEFAULT 0
is_on_critical_path BOOLEAN     DEFAULT false
blocker_ids       JSONB         DEFAULT '[]'  -- array of ticket id strings
stale_since       TIMESTAMPTZ   NULL
owner_changed_at  TIMESTAMPTZ   NULL
scope_changed     BOOLEAN       DEFAULT false
milestone_target  VARCHAR(255)  NULL
risk_flag         VARCHAR(20)   NULL          -- STALE|BLOCKED|SCOPE_CREEP|OVERLOADED
risk_severity     VARCHAR(10)   NULL          -- LOW|MEDIUM|HIGH|CRITICAL
risk_reason       TEXT          NULL
created_at        TIMESTAMPTZ   DEFAULT NOW()
updated_at        TIMESTAMPTZ   DEFAULT NOW()

INDEX: (program_id, sprint_id)
INDEX: (program_id, status)
INDEX: (assignee, status)
```

**Table: `agent_decisions`**
```
id                UUID          PRIMARY KEY DEFAULT gen_random_uuid()
program_id        UUID          NOT NULL REFERENCES programs(id)
domain            VARCHAR(100)  NOT NULL      -- copied from program.domain
run_id            VARCHAR(100)  NOT NULL      -- ties to pipeline cycle
cycle_number      INTEGER       NOT NULL
agent_name        VARCHAR(100)  NOT NULL      -- 'telemetry'|'risk_detection'|etc.
decision          TEXT          NOT NULL      -- one-line summary
reasoning         TEXT          NOT NULL      -- full reasoning trace
input_summary     JSONB                       -- key inputs that drove the decision
output_summary    JSONB                       -- key outputs produced
created_at        TIMESTAMPTZ   DEFAULT NOW()

INDEX: (program_id, run_id)
INDEX: (program_id, created_at DESC)
```

**Table: `executive_outputs`**
```
id                UUID          PRIMARY KEY DEFAULT gen_random_uuid()
program_id        UUID          NOT NULL REFERENCES programs(id)
domain            VARCHAR(100)  NOT NULL
run_id            VARCHAR(100)  NOT NULL
cycle_number      INTEGER       NOT NULL
output_type       VARCHAR(30)   NOT NULL      -- STANDUP_SUMMARY|ESCALATION_MEMO|RISK_DIGEST
content           TEXT          NOT NULL
created_at        TIMESTAMPTZ   DEFAULT NOW()

INDEX: (program_id, output_type, created_at DESC)
```

**Table: `operational_memory`**
```
id                UUID          PRIMARY KEY DEFAULT gen_random_uuid()
program_id        UUID          NOT NULL REFERENCES programs(id)
domain            VARCHAR(100)  NOT NULL
key               VARCHAR(255)  NOT NULL
value             JSONB         NOT NULL
expires_at        TIMESTAMPTZ   NULL
created_at        TIMESTAMPTZ   DEFAULT NOW()
updated_at        TIMESTAMPTZ   DEFAULT NOW()

UNIQUE: (program_id, key)
INDEX: (program_id, key)
```

---

### Redis Usage

**Caching strategy**

Key: `program:{program_id}:tickets:current`
Value: JSON-serialized list of current tickets with risk flags
TTL: 35 seconds (5-second buffer beyond agent loop interval)
Written: by Communication Agent at end of each cycle
Read: by FastAPI on initial dashboard load; avoided on WebSocket path (pushed, not pulled)

Key: `program:{program_id}:last_run_id`
Value: string UUID of most recent completed run
TTL: none (overwritten each cycle)

Key: `program:{program_id}:sprint_health:current`
Value: JSON list of sprint health records
TTL: 35 seconds
Note: PostgreSQL `sprints` table is source of truth. This key is a cache layer on top. API reads from PostgreSQL. Redis cache used only for WebSocket init message path.

**Pub/Sub channels**

Channel: `agent_stream:{program_id}`
Publisher: each agent publishes its decision entries as they are produced (not at end of cycle — enables real-time Activity Stream panel)
Message shape: `{"agent": str, "decision": str, "reasoning": str, "timestamp": str, "run_id": str}`
Subscriber: FastAPI WebSocket manager, fans out to all connected clients for that program

**Redis Streams**

Stream: `pipeline:{program_id}`
Entries published by: Communication Agent at cycle completion
Entry fields: `run_id`, `cycle_number`, `timestamp`, `ticket_count`, `risk_count`, `health_summary`
Consumer group: `dashboard_consumers`
Retention: last 200 entries (MAXLEN 200)
Purpose: ordered event log enabling replay and ensuring no dashboard client misses a cycle update on reconnect

---

### FastAPI Routes

**Programs**
```
GET  /api/programs
     Response: [{id, name, domain, is_active, created_at}]

GET  /api/programs/{program_id}
     Response: {id, name, domain, context_config, is_active, created_at}
```

**Sprints and health**
```
GET  /api/programs/{program_id}/sprints
     Response: [{sprint_id, name, pct_complete, health_badge, worst_severity,
                 ticket_count, run_id, updated_at}]
```

**Tickets**
```
GET  /api/programs/{program_id}/tickets
     Query params: team (string), severity (LOW|MEDIUM|HIGH|CRITICAL),
                   flag (STALE|BLOCKED|SCOPE_CREEP|OVERLOADED)
     Response: [{id, title, assignee, team, status, priority, sprint_id,
                 story_points, points_completed, is_on_critical_path,
                 risk_flag, risk_severity, risk_reason, updated_at}]
```

**Agent decisions**
```
GET  /api/programs/{program_id}/decisions
     Query params: run_id (string), agent_name (string),
                   limit (int, default 50), offset (int)
     Response: {total: int, items: [{id, agent_name, decision, reasoning,
                                     input_summary, output_summary, created_at}]}
```

**Executive outputs**
```
GET  /api/programs/{program_id}/outputs
     Response: {standup_summary: {content, cycle_number, created_at},
                escalation_memo: {content, cycle_number, created_at} | null,
                risk_digest: {content, cycle_number, created_at}}

GET  /api/programs/{program_id}/outputs/{output_type}
     output_type: standup_summary | escalation_memo | risk_digest
     Response: {content, cycle_number, run_id, created_at}
```

**Simulation control**
```
POST /api/simulation/{program_id}/trigger
     Body: {} (no required fields)
     Response: {run_id, cycle_number, started_at, status: "triggered"}
```

**Health**
```
GET  /health
     Response: {status: "ok", db: "ok"|"error", redis: "ok"|"error",
                last_cycle: {run_id, cycle_number, completed_at} | null}
```

---

### WebSocket Strategy

**Endpoint:** `WS /ws/{program_id}`

**Connection lifecycle:**
1. Client connects. Server subscribes client to Redis pub/sub channel `agent_stream:{program_id}`.
2. Server immediately sends `init` message containing: current sprint health, current tickets with risk flags, and most recent executive outputs from cache. Client renders full dashboard state without waiting for next cycle.
3. On each incoming pub/sub message from `agent_stream:{program_id}`, server fans out to all connected clients for that program.
4. At cycle completion, server sends a `cycle_complete` message containing updated sprint health, risk flags, and executive outputs.
5. Client disconnects → server removes from subscriber set.

**Message types (server → client):**
```
{type: "init",            data: {sprints, tickets, outputs}}
{type: "agent_decision",  data: {agent, decision, reasoning, timestamp, run_id}}
{type: "cycle_complete",  data: {run_id, cycle_number, sprints, tickets, outputs}}
{type: "error",           data: {message}}
```

**Reconnection:** On reconnect, client sends last received `run_id`. Server checks Redis Stream for any missed `cycle_complete` events and replays them before resuming live feed.

---

### Program Context Schema

Full field definitions (stored in YAML, loaded to JSONB in `programs.context_config`):

```yaml
program_id: string                    # matches programs.id
program_name: string
domain: string                        # enterprise_software | government_compliance | ...

thresholds:
  stale_ticket_days: integer          # tickets not updated for N days trigger STALE
  overload_points_per_assignee: integer  # IN_PROGRESS points ceiling per assignee
  scope_creep_story_point_increase: float  # e.g. 0.25 = 25% point increase triggers flag
  critical_path_severity_multiplier: float  # e.g. 1.5 — elevates severity for critical path tickets

sprint_health_rules:
  watch_min_flags: integer            # minimum flagged tickets to enter WATCH
  alert_requires_severity: string     # HIGH — any flag at this severity → ALERT
  escalate_requires_severity: string  # CRITICAL — any flag at this severity → ESCALATE

escalation_rules:
  trigger_severities: [HIGH, CRITICAL]  # which severities produce an Escalation Memo
  trigger_flags: [BLOCKED, STALE]       # optionally restrict to specific flag types

teams:
  - name: string                      # team name as it appears on tickets
    lead: string                      # used by Mitigation Agent for suggested_owner

milestones:
  - id: string
    name: string
    target_date: date                 # used for urgency computation in Mitigation Agent

simulation_weights:                   # optional overrides for Simulation Engine probabilities
  ticket_stalled: float               # default 0.15
  blocker_added: float                # default 0.08
  scope_expanded: float               # default 0.06
  ticket_progressed: float            # default 0.25
  blocker_resolved: float             # default 0.20
```

---

### Simulation Engine Design

**Purpose:** Mutate ticket state in PostgreSQL before each agent cycle runs, producing a realistic backlog that evolves over time.

**Execution:** Called by the agent loop before LangGraph pipeline starts. Runs as a synchronous DB transaction. Publishes event list to Redis Stream `simulation:{program_id}` after mutations complete.

**Ticket mutation event types and logic:**

| Event | Trigger condition | Mutation applied | Base probability |
|---|---|---|---|
| `TICKET_STALLED` | ticket is IN_PROGRESS, updated >2 days ago | `updated_at` frozen (not changed); `stale_since` set to `now()` if currently null | 0.15 per eligible ticket |
| `BLOCKER_ADDED` | ticket is IN_PROGRESS, has no current blockers | adds a random IN_PROGRESS ticket id to `blocker_ids`, sets status to BLOCKED | 0.08 |
| `SCOPE_EXPANDED` | ticket is TODO or IN_PROGRESS | sets `scope_changed = true`, increases `story_points` by 1–3 | 0.06 |
| `ASSIGNEE_OVERLOADED` | assignee has 15+ IN_PROGRESS points | adds another IN_PROGRESS ticket to their queue (status mutation on a TODO ticket) | computed per assignee |
| `TICKET_PROGRESSED` | ticket is IN_PROGRESS | increments `points_completed` by 1, sets `updated_at = now()` | 0.25 |
| `TICKET_COMPLETED` | ticket is IN_PROGRESS, points_completed >= story_points | sets status to DONE, `updated_at = now()` | conditional on TICKET_PROGRESSED |
| `BLOCKER_RESOLVED` | ticket is BLOCKED | removes one entry from `blocker_ids`, sets status to IN_PROGRESS if no remaining blockers | 0.20 per blocked ticket |

**Probability tuning:** Base probabilities are overrideable in Program Context config under `simulation_weights` key.

**Publishing:** After mutations, simulation engine publishes to Redis Stream `simulation:{program_id}`: `{run_id, events_applied: [{event_type, ticket_id}], timestamp}`.

---

### Agent Loop Execution Design

**Scheduler:** `asyncio` task running inside FastAPI lifespan context. Uses `asyncio.sleep(30)` in a `while True` loop. Interval configurable via `AGENT_LOOP_INTERVAL_SECONDS` env var.

**Cycle execution sequence:**
```
1. Generate run_id (UUID4), increment cycle_number (read/write key `cycle_counter` in `operational_memory`, scoped per program_id)
2. Run Simulation Engine — mutate tickets, publish simulation events
3. Initialize LangGraph PipelineState with run_id, cycle_number, program_context
4. Execute LangGraph graph: Telemetry → Risk Detection → Dependency → Mitigation → Communication
5. Communication Agent persists all outputs to PostgreSQL
6. Communication Agent writes current state to Redis cache
7. Communication Agent publishes cycle_complete to Redis pub/sub → WebSocket fans out
8. Agent loop logs cycle completion time and any errors to stdout
9. Sleep until next interval
```

**Startup behavior:** Before the agent loop begins its first cycle, the seed data loader runs: seeds tickets and sprints to PostgreSQL, then pre-populates Redis cache keys (`tickets:current`, `sprint_health:current`) from the seeded data. Dashboard renders on first load without waiting for the first 30-second cycle to complete.

**Error handling:**
- If any single agent raises an exception: error is appended to `state.errors`, that agent's outputs are set to empty/null, pipeline continues with next agent using best available state.
- If PostgreSQL is unavailable: cycle is skipped entirely, error logged, loop continues.
- If Redis is unavailable: pipeline still runs and persists to DB; WebSocket push is skipped with warning logged.
- If Claude API returns an error: Communication Agent retries once with 5-second delay. On second failure, executive_outputs are marked as `null` for the cycle. Dashboard shows "Output generation failed this cycle" with timestamp.
- All errors logged to stdout in structured JSON format: `{level, agent, run_id, cycle_number, error, timestamp}`.

**Logging:** Every agent decision written to `agent_decisions` table within the LangGraph node execution, not batched at end. This means Activity Stream panel receives live decisions via WebSocket as each agent completes, not all at once at end of cycle.

---

### Directory Structure

```
autonomous-tpm/
├── backend/
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── telemetry.py           # Telemetry Agent node function
│   │   ├── risk_detection.py      # Risk Detection Agent node function
│   │   ├── dependency_analysis.py # Dependency Analysis Agent node function
│   │   ├── mitigation.py          # Mitigation Agent node function
│   │   └── communication.py       # Communication Agent node function + Claude API calls
│   ├── core/
│   │   ├── __init__.py
│   │   ├── pipeline.py            # LangGraph StateGraph definition and compilation
│   │   ├── state.py               # PipelineState TypedDict, all nested types
│   │   ├── loop.py                # asyncio agent loop, scheduler
│   │   └── program_context.py     # ProgramContext dataclass, YAML loader
│   ├── db/
│   │   ├── __init__.py
│   │   ├── models.py              # SQLAlchemy ORM models
│   │   ├── session.py             # async session factory, get_db dependency
│   │   └── migrations/
│   │       ├── env.py
│   │       ├── alembic.ini
│   │       └── versions/          # Alembic migration files
│   ├── simulation/
│   │   ├── __init__.py
│   │   └── engine.py              # Ticket mutation logic, event publishing
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes/
│   │   │   ├── programs.py
│   │   │   ├── tickets.py
│   │   │   ├── decisions.py
│   │   │   ├── outputs.py
│   │   │   └── simulation.py
│   │   └── websocket.py           # WebSocket endpoint + connection manager
│   ├── redis/
│   │   ├── __init__.py
│   │   ├── client.py              # Redis connection factory
│   │   ├── cache.py               # Cache read/write helpers
│   │   ├── streams.py             # Redis Streams publish/consume helpers
│   │   └── pubsub.py              # Pub/sub publish + WebSocket fan-out
│   ├── config/
│   │   └── programs/
│   │       └── default.yaml       # Default program context (enterprise_software domain)
│   ├── main.py                    # FastAPI app, lifespan, route registration
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── ProgramHealthBar.jsx
│   │   │   ├── BacklogView.jsx
│   │   │   ├── AgentActivityStream.jsx
│   │   │   └── ExecutiveOutputPanel.jsx
│   │   ├── hooks/
│   │   │   └── useWebSocket.js    # WebSocket connection, reconnect logic, run_id tracking
│   │   ├── App.jsx
│   │   └── main.jsx
│   ├── index.html
│   ├── package.json
│   └── vite.config.js
├── docker-compose.yml             # PostgreSQL + Redis services
├── .env.example
└── README.md
```

---

### Environment Variables

```
# Required
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/atis
REDIS_URL=redis://localhost:6379/0
ANTHROPIC_API_KEY=sk-ant-...
DEFAULT_PROGRAM_ID=<UUID of seeded default program>

# Agent loop
AGENT_LOOP_INTERVAL_SECONDS=30     # default: 30

# FastAPI
CORS_ORIGINS=http://localhost:5173  # Vite dev server; comma-separated for multiple
API_HOST=0.0.0.0
API_PORT=8000

# Logging
LOG_LEVEL=INFO                      # DEBUG|INFO|WARNING|ERROR

# Redis TTL
REDIS_CACHE_TTL_SECONDS=35          # should exceed AGENT_LOOP_INTERVAL_SECONDS

# Claude API
CLAUDE_MODEL=claude-sonnet-4-6
CLAUDE_MAX_TOKENS=4096   # Communication Agent calls only.
                         # Standup summary + escalation memo
                         # require headroom. Do not lower below 2048.
```
