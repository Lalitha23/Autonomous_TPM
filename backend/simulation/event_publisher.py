"""
Simulation Event Publisher — writes mutation events to a Redis Stream.

Stream key: streams:sim_events:{program_id}
MAXLEN:     500 (approximate, using ~ trimming for performance)

Each event is stored as a Redis Stream entry with flat string fields.
Consumers (Telemetry Agent) read via XREAD or XREADGROUP.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_STREAM_MAXLEN = 500


def _stream_key(program_id: str) -> str:
    return f"streams:sim_events:{program_id}"


def _flatten_event(event: Dict[str, Any]) -> Dict[str, str]:
    """
    Redis Stream entries must be flat string→string dicts.
    Serialize any non-string values (lists, ints) to JSON strings.
    """
    flat: Dict[str, str] = {}
    for k, v in event.items():
        if isinstance(v, str):
            flat[k] = v
        else:
            flat[k] = json.dumps(v)
    return flat


async def publish_events(
    redis_client: aioredis.Redis,
    program_id: str,
    events: List[Dict[str, Any]],
) -> int:
    """
    Write a list of mutation events to the simulation Redis Stream.

    Args:
        redis_client:  An open async Redis client.
        program_id:    The program identifier (used to namespace the stream key).
        events:        List of event dicts produced by engine.mutate_state().

    Returns:
        Number of events successfully written.

    Raises:
        redis.exceptions.RedisError: propagated on connection or write failure.
    """
    if not events:
        return 0

    key = _stream_key(program_id)
    written = 0

    for event in events:
        flat = _flatten_event(event)
        await redis_client.xadd(
            key,
            flat,
            maxlen=_STREAM_MAXLEN,
            approximate=True,  # ~ trimming — lower overhead than exact
        )
        written += 1

    logger.debug(
        "Published %d simulation events to stream '%s'",
        written,
        key,
    )
    return written
