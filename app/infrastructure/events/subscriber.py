"""Redis pub/sub -> local WebSocket fan-out (API side).

Every API replica subscribes to the same channel, so an event published by any
replica (or by a Celery worker) reaches the browsers connected to all of them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from uuid import UUID

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_MAX_BACKOFF_SECONDS = 30.0


class EventSubscriber:
    def __init__(
        self,
        redis: Redis,
        channel: str,
        on_event: Callable[[str, UUID | None], None],
        on_resync: Callable[[], None],
    ) -> None:
        self._redis = redis
        self._channel = channel
        self._on_event = on_event
        self._on_resync = on_resync

    async def run(self) -> None:
        """Subscribe forever; reconnect with exponential backoff.

        Pub/sub is fire-and-forget: anything published while we were disconnected is
        gone. After an outage we therefore emit `system.resync`, telling clients to
        refetch the REST snapshot instead of trusting an event stream with a gap.
        """
        backoff = 1.0
        had_outage = False
        while True:
            try:
                async with self._redis.pubsub() as pubsub:
                    await pubsub.subscribe(self._channel)
                    logger.info("events.subscribed", extra={"channel": self._channel})
                    if had_outage:
                        self._on_resync()
                        had_outage = False
                    backoff = 1.0
                    async for message in pubsub.listen():
                        if message["type"] == "message":
                            self._dispatch(message["data"])
            except asyncio.CancelledError:
                raise
            except Exception:
                had_outage = True
                logger.exception("events.subscriber_failed", extra={"retry_in": backoff})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

    def _dispatch(self, raw: str) -> None:
        try:
            room_id_raw = json.loads(raw).get("room_id")
            room_id = UUID(room_id_raw) if room_id_raw else None
        except (ValueError, AttributeError, TypeError):
            logger.warning("events.malformed_message")
            return
        self._on_event(raw, room_id)
