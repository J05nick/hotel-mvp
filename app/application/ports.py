"""Ports (interfaces) the application layer depends on.

Adapters in `app.infrastructure` implement them; the use cases never import
SQLAlchemy, Redis or FastAPI.
"""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from app.domain.entities import Booking, Room
from app.domain.events import DomainEvent
from app.domain.value_objects import TimeSlot


class RoomRepository(Protocol):
    async def get(self, room_id: UUID) -> Room | None: ...

    async def list_all(self) -> list[Room]:
        """Every room ordered by number."""
        ...

    async def get_for_update(self, room_id: UUID, *, skip_locked: bool = False) -> Room | None:
        """`SELECT ... FOR UPDATE [SKIP LOCKED]`. With `skip_locked`, returns None when
        another transaction holds the row (the caller treats it as "someone else is on it")."""
        ...

    async def save(self, room: Room) -> None:
        """Persist status changes of a room loaded in this unit of work."""
        ...

    async def get_for_share(self, room_id: UUID) -> Room | None:
        """`SELECT ... FOR SHARE`: concurrent bookers coexist, but the room row
        cannot be changed (maintenance, cleaning) until this transaction ends."""
        ...


class BookingRepository(Protocol):
    async def add(self, booking: Booking) -> None:
        """Insert and flush.

        Raises SlotUnavailableError on an exclusion-constraint violation and
        DuplicateIdempotencyKeyError when the Idempotency-Key already exists.
        """
        ...

    async def get(self, booking_id: UUID) -> Booking | None: ...

    async def get_for_update(self, booking_id: UUID) -> Booking | None:
        """`SELECT ... FOR UPDATE`: pessimistic row lock for state transitions."""
        ...

    async def get_by_idempotency_key(self, key: str) -> Booking | None: ...

    async def save(self, booking: Booking) -> None:
        """Persist status / hold changes of a booking loaded in this unit of work."""
        ...

    async def list_visible_in_window(
        self, start: datetime, end: datetime, now: datetime
    ) -> list[Booking]:
        """Bookings overlapping [start, end) that occupy or occupied the calendar.

        PENDING / CONFIRMED / CHECKED_IN / COMPLETED; lapsed unconfirmed holds,
        cancelled and expired bookings are not visible.
        """
        ...

    async def rooms_with_finished_bookings(self, now: datetime, limit: int) -> list[UUID]:
        """Rooms having CONFIRMED / CHECKED_IN bookings whose range already ended."""
        ...

    async def list_finished_for_update(self, room_id: UUID, now: datetime) -> list[Booking]:
        """Ended CONFIRMED / CHECKED_IN bookings of the room, row-locked with SKIP LOCKED."""
        ...

    async def has_active_booking(self, room_id: UUID, now: datetime) -> bool:
        """True when a CONFIRMED / CHECKED_IN booking covers `now` in this room."""
        ...

    async def list_expired_holds_for_update(self, now: datetime, limit: int) -> list[Booking]:
        """PENDING bookings whose hold lapsed, row-locked with SKIP LOCKED."""
        ...

    async def expire_stale_holds(
        self, room_id: UUID, slot: TimeSlot, now: datetime
    ) -> list[Booking]:
        """Expire PENDING bookings of the room overlapping `slot` whose hold lapsed."""
        ...


class UnitOfWork(Protocol):
    # Read-only properties: lets adapters expose their concrete repository types.
    @property
    def rooms(self) -> RoomRepository: ...

    @property
    def bookings(self) -> BookingRepository: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Rolls back anything not committed and releases the connection."""
        ...

    async def commit(self) -> None: ...


class SlotHoldStore(Protocol):
    """Temporary "cart" holds on 30-minute blocks (Redis, TTL-based)."""

    async def acquire(self, room_id: UUID, slot: TimeSlot, owner: UUID, ttl_seconds: int) -> bool:
        """Hold every block of `slot` for `owner`; all-or-nothing."""
        ...

    async def release(self, room_id: UUID, slot: TimeSlot, owner: UUID) -> None:
        """Release only the blocks still owned by `owner` (compare-and-delete)."""
        ...


class EventPublisher(Protocol):
    async def publish(self, events: list[DomainEvent]) -> None:
        """Best-effort fan-out; must never raise for transport failures."""
        ...
