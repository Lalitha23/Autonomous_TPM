"""
FastAPI entry point — Autonomous TPM Intelligence System.

Startup sequence (lifespan):
1. Load ProgramContext from config/programs/default.yaml.
2. Seed program to PostgreSQL + Redis (idempotent).
3. Launch agent loop as a background asyncio task.

Routes:
  GET /health  — liveness + dependency check.
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
from sqlalchemy import text

from agents.runner import start_agent_loop
from core.context_loader import load_context
from db.session import AsyncSessionLocal, engine
from simulation.seeder import seed_program

logger = logging.getLogger(__name__)

DEFAULT_YAML = Path(__file__).parent / "config" / "programs" / "default.yaml"

# Module-level state shared between lifespan and route handlers
_program_id: Optional[str] = None
_agent_task: Optional[asyncio.Task] = None
_redis_client: Optional[aioredis.Redis] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown lifecycle."""
    global _program_id, _agent_task, _redis_client

    # ── Startup ───────────────────────────────────────────────────────────
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

    yield  # Application runs here

    # ── Shutdown ──────────────────────────────────────────────────────────
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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Liveness and dependency check.

    Returns:
        {
          "status": "ok",
          "db": "ok" | "error",
          "redis": "ok" | "error",
          "last_cycle": null   # populated in a future stage
        }
    """
    # Database check
    db_status = "ok"
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        logger.exception("/health: database check failed")
        db_status = "error"

    # Redis check
    redis_status = "ok"
    try:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = aioredis.from_url(redis_url)
        await r.ping()
        await r.aclose()
    except Exception:
        logger.exception("/health: Redis check failed")
        redis_status = "error"

    return {
        "status": "ok",
        "db": db_status,
        "redis": redis_status,
        "last_cycle": None,
    }
