"""
WebSocket endpoint — Stage 7.

WS /ws/{program_id}

Protocol:
  On connect:
    1. Accept connection, optionally receive last_run_id query param for replay.
    2. Send {type: "init", data: {sprints, tickets, outputs}} from Redis cache
       (falls back to PostgreSQL on cold start).
    3. Replay any missed cycle_complete events from Redis Stream since last_run_id.
    4. Subscribe to Redis pub/sub channel `agent_stream:{program_id}`.
    5. Forward all incoming pub/sub messages to the client as-is.

  While connected:
    - Incoming pub/sub messages are forwarded to the WebSocket client.
    - The runner publishes {type: "cycle_complete", data: {...}} after each cycle.

  On disconnect:
    - Unsubscribe from pub/sub, close Redis connection.

Message shapes:
  {type: "init",           data: {sprints, tickets, outputs}}
  {type: "cycle_complete", data: {run_id, cycle_number, sprints, tickets, outputs, decisions}}
  {type: "agent_decision", data: {agent_name, decision, reasoning, timestamp, run_id}}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import desc, select

from db.models import AgentDecision, ExecutiveOutput, Program, Sprint, Ticket
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

ws_router = APIRouter()

_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_OUTPUT_TYPE_MAP = {
    "STANDUP_SUMMARY": "standup_summary",
    "ESCALATION_MEMO": "escalation_memo",
    "RISK_DIGEST":     "risk_digest",
}


# ── Init data builder ─────────────────────────────────────────────────────────

async def _build_init_data(program_id: str) -> dict:
    """
    Build the init payload.
    Tries Redis cache first (sprint_health:current, tickets:current).
    Falls back to PostgreSQL queries on cache miss.
    """
    import uuid as _uuid_mod
    pid = _uuid_mod.UUID(program_id)

    r = aioredis.from_url(_REDIS_URL)
    sprints = None
    tickets = None

    try:
        raw_sprints = await r.get(f"sprint_health:current:{program_id}")
        raw_tickets = await r.get(f"tickets:current:{program_id}")
        if raw_sprints:
            sprints = json.loads(raw_sprints)
        if raw_tickets:
            tickets = json.loads(raw_tickets)
    except Exception:
        logger.exception("Redis cache read failed in WS init — falling back to DB.")
    finally:
        await r.aclose()

    # Fall back to PostgreSQL
    if sprints is None:
        async with AsyncSessionLocal() as db:
            from sqlalchemy import func
            sprints_result = await db.execute(
                select(Sprint).where(Sprint.program_id == pid)
            )
            db_sprints = sprints_result.scalars().all()

            agg_result = await db.execute(
                select(
                    Ticket.sprint_id,
                    func.coalesce(func.sum(Ticket.story_points), 0).label("total"),
                    func.coalesce(func.sum(Ticket.points_completed), 0).label("completed"),
                )
                .where(Ticket.program_id == pid)
                .group_by(Ticket.sprint_id)
            )
            agg = {row.sprint_id: row for row in agg_result}

            sprints = []
            for s in db_sprints:
                a = agg.get(s.id)
                total = int(a.total) if a else 0
                done = int(a.completed) if a else 0
                sprints.append({
                    "sprint_id": s.id,
                    "name": s.name,
                    "pct_complete": round((done / total * 100) if total > 0 else 0, 1),
                    "health_badge": s.health_badge,
                    "worst_severity": s.worst_severity,
                })

    if tickets is None:
        async with AsyncSessionLocal() as db:
            t_result = await db.execute(
                select(Ticket).where(Ticket.program_id == pid)
            )
            db_tickets = t_result.scalars().all()
            tickets = [
                {
                    "id": t.id,
                    "title": t.title,
                    "assignee": t.assignee,
                    "team": t.team,
                    "status": t.status,
                    "priority": t.priority,
                    "sprint_id": t.sprint_id,
                    "story_points": t.story_points,
                    "points_completed": t.points_completed,
                    "is_on_critical_path": t.is_on_critical_path,
                    "risk_flag": t.risk_flag,
                    "risk_severity": t.risk_severity,
                    "risk_reason": t.risk_reason,
                    "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                }
                for t in db_tickets
            ]

    # Latest outputs from PostgreSQL (always fresh)
    outputs: dict[str, Optional[dict]] = {
        "standup_summary": None,
        "escalation_memo": None,
        "risk_digest": None,
    }
    async with AsyncSessionLocal() as db:
        for db_type, key in _OUTPUT_TYPE_MAP.items():
            result = await db.execute(
                select(ExecutiveOutput)
                .where(
                    ExecutiveOutput.program_id == pid,
                    ExecutiveOutput.output_type == db_type,
                )
                .order_by(desc(ExecutiveOutput.created_at))
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row:
                outputs[key] = {
                    "content": row.content,
                    "cycle_number": row.cycle_number,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }

    return {"sprints": sprints, "tickets": tickets, "outputs": outputs}


async def _replay_missed_events(
    ws: WebSocket,
    program_id: str,
    last_run_id: Optional[str],
) -> None:
    """
    Read Redis Stream `pipeline:{program_id}` for events since last_run_id.
    Send each as a cycle_complete message before resuming live feed.
    """
    if not last_run_id:
        return

    r = aioredis.from_url(_REDIS_URL)
    try:
        # Use ">" to get unread messages, or read from a specific ID.
        # We use "0-0" to get ALL stream entries and filter by run_id.
        entries = await r.xrange(f"pipeline:{program_id}", min="-", max="+", count=50)
        seen_run_ids: set[str] = set()

        for entry_id, fields in entries:
            event_type = fields.get(b"event", b"").decode()
            run_id = fields.get(b"run_id", b"").decode()

            # Skip everything up to and including the last_run_id the client saw
            if run_id == last_run_id:
                seen_run_ids.add(run_id)
                continue
            if not seen_run_ids:
                # Haven't found last_run_id yet — keep skipping
                if run_id != last_run_id:
                    continue

            if event_type == "cycle_complete" and run_id not in seen_run_ids:
                seen_run_ids.add(run_id)
                cycle_number = int(fields.get(b"cycle_number", b"0").decode())
                try:
                    await ws.send_json({
                        "type": "cycle_complete",
                        "data": {
                            "run_id": run_id,
                            "cycle_number": cycle_number,
                            "replayed": True,
                        },
                    })
                except Exception:
                    break
    except Exception:
        logger.exception("Error replaying missed events for program=%s", program_id)
    finally:
        await r.aclose()


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@ws_router.websocket("/ws/{program_id}")
async def websocket_endpoint(
    ws: WebSocket,
    program_id: str,
    last_run_id: Optional[str] = None,
):
    await ws.accept()
    logger.info("WS connect: program=%s last_run_id=%s", program_id, last_run_id)

    # 1. Send init data
    try:
        init_data = await _build_init_data(program_id)
        await ws.send_json({"type": "init", "data": init_data})
    except Exception:
        logger.exception("WS init data build failed for program=%s", program_id)
        await ws.close(code=1011)
        return

    # 2. Replay missed events
    await _replay_missed_events(ws, program_id, last_run_id)

    # 3. Subscribe to Redis pub/sub
    r = aioredis.from_url(_REDIS_URL)
    pubsub = r.pubsub()
    channel = f"agent_stream:{program_id}"
    await pubsub.subscribe(channel)
    logger.info("WS subscribed to pub/sub channel: %s", channel)

    # 4. Run two concurrent tasks: forward pub/sub → client, and detect client disconnect
    async def _forward_pubsub():
        """Listen to Redis pub/sub and forward messages to the WebSocket client."""
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    payload = json.loads(data)
                    await ws.send_json(payload)
                except Exception:
                    logger.debug("WS send failed — client likely disconnected.")
                    break

    async def _watch_client():
        """Block until client disconnects or sends any message."""
        try:
            while True:
                await ws.receive_text()  # ignore content; just keep alive
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    pubsub_task = asyncio.create_task(_forward_pubsub())
    client_task = asyncio.create_task(_watch_client())

    try:
        done, pending = await asyncio.wait(
            {pubsub_task, client_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    except Exception:
        logger.exception("WS event loop error for program=%s", program_id)
    finally:
        try:
            await pubsub.unsubscribe(channel)
        except Exception:
            pass
        try:
            await r.aclose()
        except Exception:
            pass
        logger.info("WS disconnect: program=%s", program_id)
