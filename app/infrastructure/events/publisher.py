"""Redis pub/sub event publisher (API side)."""

from __future__ import annotations

import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.domain.events import DomainEvent
from app.infrastructure.events.wire import to_wire

logger = logging.getLogger(__name__)


class RedisEventPublisher:
    """Publishes committed domain events to every API replica via one channel.

    Failures are logged, not raised: the database transaction already committed,
    and clients recover through the `system.resync` + REST snapshot path.
    """

    def __init__(self, redis: Redis, channel: str) -> None:
        self._redis = redis
        self._channel = channel

    async def publish(self, events: list[DomainEvent]) -> None:
        if not events:
            return
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for event in events:
                    pipe.publish(self._channel, to_wire(event).model_dump_json())
                await pipe.execute()
        except RedisError:
            logger.exception("events.publish_failed", extra={"count": len(events)})
