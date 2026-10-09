"""Redis connection factory (locks, cache and pub/sub share logical DB 0)."""

from __future__ import annotations

from functools import lru_cache

from redis.asyncio import Redis

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    # A single client owns a connection pool; reuse it across the process.
    client: Redis = Redis.from_url(
        str(get_settings().redis_url),
        decode_responses=True,
        health_check_interval=30,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    return client
