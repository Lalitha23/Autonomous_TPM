# Autonomous TPM Intelligence System (ATIS)

A multi-agent platform that monitors a simulated software backlog, detects TPM-class risks autonomously every 30 seconds, and generates executive-quality standup summaries, escalation memos, and risk digests — no human prompting required.

---

## What It Does

ATIS runs a five-agent LangGraph pipeline on a 30-second autonomous loop:

| Agent | Responsibility |
|---|---|
| **Telemetry** | Reads ticket/sprint state from PostgreSQL, computes sprint velocity and stale tickets |
| **Risk Detection** | Flags BLOCKED, STALE, OVERLOADED, and SCOPE_CREEP conditions with severity ladder (LOW → CRITICAL) |
| **Dependency Analysis** | Traces blocker chains and computes blast radius for each risk |
| **Mitigation** | Generates rule-based action plans with named owners and urgency tiers |
| **Communication** | Calls Claude API (`claude-sonnet-4-6`) to write standup summary, escalation memo, and risk digest; graceful fallback when API key is absent |

The dashboard is display-only — all computation runs in agents, all decisions are logged with full reasoning traces.

---

## Prerequisites

- **Docker Desktop** (running)
- **Python 3.11** with `venv`
- **Node.js 18+**
- `.env` file in `backend/` (see Setup)

---

## Setup

### 1. Start PostgreSQL + Redis

```bash
docker compose up -d
```

### 2. Backend

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Apply database migrations
alembic upgrade head

# Start the API server (seeds DB + starts agent loop automatically)
PYTHONPATH=. uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

**Environment variables** — create `backend/.env`:

```
DATABASE_URL=postgresql+asyncpg://atis:atis@localhost:5432/atis
REDIS_URL=redis://localhost:6379
ANTHROPIC_API_KEY=sk-ant-...        # Optional — system uses fallback if absent
CLAUDE_MAX_TOKENS=4096
PROGRAM_ID=                          # Auto-populated on first seed run
```

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

Dashboard opens at **http://localhost:5173**

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  React Dashboard                    │
│  Program Health │ Backlog │ Agent Activity │ Outputs │
└────────────────────┬────────────────────────────────┘
                     │ WebSocket (ws://localhost:8000)
┌────────────────────▼────────────────────────────────┐
│              FastAPI + uvicorn                      │
│  REST: /api/programs /api/.../sprints /tickets …   │
│  WS:   /ws/{program_id}                            │
└────────────────────┬────────────────────────────────┘
              ┌──────┴──────┐
              │             │
     ┌────────▼──────┐  ┌───▼───────────┐
     │  PostgreSQL   │  │     Redis      │
     │  (source of   │  │  cache + pub/  │
     │   truth)      │  │  sub streams   │
     └────────┬──────┘  └───────────────┘
              │
┌─────────────▼───────────────────────────────────────┐
│         LangGraph Agent Pipeline (30s loop)         │
│                                                     │
│  Telemetry → Risk Detection → Dependency Analysis  │
│           → Mitigation → Communication             │
└─────────────────────────────────────────────────────┘
```

**Data flow:**

1. Simulation Engine mutates tickets (status changes, blockers, stale timestamps)  
2. Agent loop fires every 30 seconds: reads DB → detects risks → traces blast radius → generates mitigations → writes executive outputs
3. Results published to Redis pub/sub → WebSocket pushes to dashboard
4. All agent decisions persisted to `agent_decisions` table with full reasoning trace

---

## Crisis Demo

Inject a realistic crisis scenario (3 blocked tickets, overloaded team) and trigger an immediate cycle:

```bash
# Get your program ID
curl http://localhost:8000/api/programs

# Inject crisis
curl -X POST http://localhost:8000/api/simulation/{program_id}/inject-crisis
```

Expected response:

```json
{
  "injected_blocks": ["ATLAS-004", "ATLAS-019", "ATLAS-024"],
  "injected_stale":  ["ATLAS-004", "ATLAS-019"],
  "injected_overload": ["ATLAS-029"],
  "blocker_map": { "ATLAS-004": ["ATLAS-029"], ... },
  "cycle_triggered": true,
  "risk_flags_detected": 27
}
```

Read the generated escalation memo:

```bash
curl http://localhost:8000/api/programs/{program_id}/outputs | python3 -m json.tool
```

---

## Swapping Program Context

ATIS is domain-extensible. The `program_context_config` in the `programs` table drives all agent behavior — thresholds, team names, milestone dates, and escalation paths are configuration, not code.

To run against a different program context, update the `context_config` JSON column and restart the agent loop. No agent code changes required.

---

## Key API Endpoints

| Method | Route | Description |
|---|---|---|
| GET | `/api/programs` | List programs |
| GET | `/api/programs/{id}/sprints` | Sprint health with badges |
| GET | `/api/programs/{id}/tickets` | All tickets with risk flags |
| GET | `/api/programs/{id}/outputs` | Latest executive outputs |
| GET | `/api/programs/{id}/decisions` | Agent decision log |
| POST | `/api/programs/{id}/cycle` | Trigger manual cycle |
| POST | `/api/simulation/{id}/inject-crisis` | Inject crisis scenario |
| WS | `/ws/{program_id}` | Real-time dashboard stream |

---

## Tech Stack

| Layer | Technology | Why |
|---|---|---|
| Agents | LangGraph | Stateful graph execution with typed state |
| AI | Claude API (`claude-sonnet-4-6`) | Best-in-class document generation; graceful fallback |
| Backend | FastAPI + uvicorn | Async-native, WebSocket support, Pydantic v2 |
| Database | PostgreSQL 16 + SQLAlchemy async + Alembic | Source of truth, full audit trail |
| Cache/Streaming | Redis 7 + Redis Streams | WebSocket init path, pub/sub for real-time push |
| Frontend | React + Vite | Fast HMR, no framework overhead |
| Container | Docker Compose | Single-command infra startup |

---

## Running Tests

```bash
cd backend
source .venv/bin/activate
pytest tests/ -v
```

All 45 tests across 5 agent modules should pass.

---

## Constraints (v1)

- No ML models — all risk detection is rule-based
- No Kafka — Redis Streams for event pipeline
- Mitigation Agent is fully rule-based (no Claude API call)
- PostgreSQL is source of truth; Redis is cache layer only
- Simulation Engine owns `stale_since` writes; Telemetry Agent only reads
