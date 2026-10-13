"""Celery tasks scheduled by Beat (see `celery_app.beat_schedule`).

The use cases are async (shared with the API), so each task runs them in its own
`asyncio.run` with a private engine and Redis client that are disposed afterwards:
nothing loop-bound leaks between invocations of a prefork worker.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.housekeeping_service import HousekeepingService
from app.core.config import get_settings
from app.infrastructure.db.repositories import SqlAlchemyUnitOfWork
from app.infrastructure.db.session import build_task_engine
from app.infrastructure.events.publisher import RedisEventPublisher
from app.infrastructure.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

COMPLETE_FINISHED_BOOKINGS: Final[str] = "housekeeping.complete_finished_bookings"
EXPIRE_STALE_HOLDS: Final[str] = "housekeeping.expire_stale_holds"

# Beat enqueues every 60 s (and expires queued runs after 55 s: if the worker was down,
# the next tick does the same work). Overlapping runs are safe: SKIP LOCKED + idempotent.
_TIME_LIMIT_SECONDS: Final[int] = 55
_SOFT_TIME_LIMIT_SECONDS: Final[int] = 50


async def _with_service[T](action: Callable[[HousekeepingService], Awaitable[T]]) -> T:
    settings = get_settings()
    engine = build_task_engine(settings)
    redis: Redis = Redis.from_url(
        str(settings.redis_url), decode_responses=True, socket_timeout=5, socket_connect_timeout=5
    )
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        service = HousekeepingService(
            uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
            events=RedisEventPublisher(redis, settings.events_channel),
            clock=lambda: datetime.now(UTC),
        )
        return await action(service)
    finally:
        await redis.aclose()
        await engine.dispose()


@celery_app.task(
    name=COMPLETE_FINISHED_BOOKINGS,
    acks_late=True,
    ignore_result=True,
    time_limit=_TIME_LIMIT_SECONDS,
    soft_time_limit=_SOFT_TIME_LIMIT_SECONDS,
)
def complete_finished_bookings() -> dict[str, int]:
    """Finished bookings -> COMPLETED, their room -> CLEANING, emit WebSocket events."""
    report = asyncio.run(_with_service(lambda svc: svc.complete_finished_bookings()))
    return {
        "bookings_completed": report.bookings_completed,
        "rooms_set_cleaning": report.rooms_set_cleaning,
        "rooms_skipped": report.rooms_skipped,
    }


@celery_app.task(
    name=EXPIRE_STALE_HOLDS,
    acks_late=True,
    ignore_result=True,
    time_limit=_TIME_LIMIT_SECONDS,
    soft_time_limit=_SOFT_TIME_LIMIT_SECONDS,
)
def expire_stale_holds() -> int:
    """PENDING bookings whose 5-minute hold lapsed -> EXPIRED."""
    return asyncio.run(_with_service(lambda svc: svc.expire_stale_holds()))
