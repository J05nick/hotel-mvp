"""Test doubles shared by integration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.domain.events import DomainEvent
from app.domain.value_objects import TimeSlot

T0 = datetime(2031, 3, 1, 10, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0 - timedelta(days=1)) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def publish(self, events: list[DomainEvent]) -> None:
        self.events.extend(events)

    def types(self) -> list[str]:
        return [e.type.value for e in self.events]


class AlwaysAcquireHoldStore:
    """Simulates Redis being down/flushed: the DB constraint must still protect us."""

    async def acquire(self, room_id: UUID, slot: TimeSlot, owner: UUID, ttl_seconds: int) -> bool:
        return True

    async def release(self, room_id: UUID, slot: TimeSlot, owner: UUID) -> None:
        return None
