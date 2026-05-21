"""
FastAPI entry point — Autonomous TPM Intelligence System.

Startup sequence (lifespan):
1. Load ProgramContext from config/programs/default.yaml.
2. Seed program to PostgreSQL + Redis (idempotent).
3. Launch agent loop as a background asyncio task.

Routes:
  GET  /health                                           — liveness + dependency check
  GET  /api/programs                                     — list programs
  GET  /api/programs/{id}                                — single program
  GET  /api/programs/{id}/sprints                        — sprint health
  GET  /api/programs/{id}/tickets                        — ticket backlog
  GET  /api/programs/{id}/decisions                      — agent decision log
  GET  /api/programs/{id}/outputs                        — latest executive outputs
  GET  /api/programs/{id}/outputs/{type}                 — specific output type
  POST /api/simulation/{id}/trigger                      — manual cycle trigger
  WS   /ws/{id}                                          — live dashboard feed
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import desc, select, text

from agents.runner import start_agent_loop
from api.routes import router as api_router
from api.websocket import ws_router
from core.context_loader import load_context
from db.models import AgentDecision
from db.session import AsyncSessionLocal, engine
from simulation.seeder import seed_program

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEFAULT_YAML = Path(__file__).parent / "config" / "programs" / "default.yaml"

_program_id: Optional[str] = None
_agent_task: Optional[asyncio.Task] = None
_redis_client: Optional[aioredis.Redis] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown lifecycle."""
    global _program_id, _agent_task, _redis_client

    logger.info("ATIS startup: loading program context from %s", DEFAULT_YAML)
    ctx = load_context(str(DEFAULT_YAML))

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    _redis_client = aioredis.from_url(redis_url)

    async with AsyncSessionLocal() as db:
        program_uuid = await seed_program(db, _redis_client, ctx)

    _program_id = str(program_uuid)
    logger.info("ATIS startup: program seeded — id=%s", _program_id)

    _agent_task = asyncio.create_task(
        start_agent_loop(_program_id, ctx),
        name="agent_loop",
    )
    logger.info("ATIS startup: agent loop task created.")

    yield

    logger.info("ATIS shutdown: cancelling agent loop.")
    if _agent_task and not _agent_task.done():
        _agent_task.cancel()
        try:
            await _agent_task
        except asyncio.CancelledError:
            pass

    if _redis_client:
        await _redis_client.aclose()

    await engine.dispose()
    logger.info("ATIS shutdown: complete.")


app = FastAPI(
    title="Autonomous TPM Intelligence System",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(api_router)
app.include_router(ws_router)


# ── Health route ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Liveness and dependency check.
    Returns last_cycle from the most recent AgentDecision row.
    """
    db_status = "ok"
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        logger.exception("/health: database check failed")
        db_status = "error"

    redis_status = "ok"
    try:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = aioredis.from_url(redis_url)
        await r.ping()
        await r.aclose()
    except Exception:
        logger.exception("/health: Redis check failed")
        redis_status = "error"

    last_cycle = None
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AgentDecision)
                .order_by(desc(AgentDecision.created_at))
                .limit(1)
            )
            latest = result.scalar_one_or_none()
            if latest:
                last_cycle = {
                    "run_id": latest.run_id,
                    "cycle_number": latest.cycle_number,
                    "completed_at": latest.created_at.isoformat() if latest.created_at else None,
                }
    except Exception:
        logger.exception("/health: last_cycle lookup failed")

    return {
        "status": "ok",
        "db": db_status,
        "redis": redis_status,
        "last_cycle": last_cycle,
    }
