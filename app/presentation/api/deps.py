"""Composition of the application services for FastAPI's dependency injection."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.application.booking_service import BookingService
from app.application.calendar_service import CalendarService
from app.application.room_service import RoomService
from app.core.config import get_settings
from app.infrastructure.cache.redis_client import get_redis
from app.infrastructure.cache.slot_holds import RedisSlotHoldStore
from app.infrastructure.db.repositories import SqlAlchemyUnitOfWork
from app.infrastructure.db.session import get_async_sessionmaker
from app.infrastructure.events.publisher import RedisEventPublisher


def _utc_now() -> datetime:
    return datetime.now(UTC)


@lru_cache(maxsize=1)
def get_booking_service() -> BookingService:
    """Stateless service: safe to share. Each use case opens its own UnitOfWork."""
    settings = get_settings()
    redis = get_redis()
    session_factory = get_async_sessionmaker()
    return BookingService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
        holds=RedisSlotHoldStore(redis),
        events=RedisEventPublisher(redis, settings.events_channel),
        hold_ttl_seconds=settings.booking_hold_ttl_seconds,
        clock=_utc_now,
    )


@lru_cache(maxsize=1)
def get_room_service() -> RoomService:
    settings = get_settings()
    session_factory = get_async_sessionmaker()
    return RoomService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
        events=RedisEventPublisher(get_redis(), settings.events_channel),
        clock=_utc_now,
    )


@lru_cache(maxsize=1)
def get_calendar_service() -> CalendarService:
    session_factory = get_async_sessionmaker()
    return CalendarService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory), clock=_utc_now
    )


BookingServiceDep = Annotated[BookingService, Depends(get_booking_service)]
RoomServiceDep = Annotated[RoomService, Depends(get_room_service)]
CalendarServiceDep = Annotated[CalendarService, Depends(get_calendar_service)]
