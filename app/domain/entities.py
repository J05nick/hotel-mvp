"""Domain entities (persistence-agnostic).

Repositories map between these dataclasses and the SQLAlchemy models
in `app.infrastructure.db.models`, keeping the domain free of ORM concerns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from app.domain.enums import BLOCKING_BOOKING_STATUSES, BookingStatus, RoomStatus
from app.domain.exceptions import InvalidStateTransitionError
from app.domain.value_objects import TimeSlot

# Explicit state machines — any transition not listed here is a bug.
ROOM_TRANSITIONS: Final[dict[RoomStatus, frozenset[RoomStatus]]] = {
    # AVAILABLE -> CLEANING: a finished time-block booking leaves the room dirty even
    # though no explicit check-in ever marked it OCCUPIED (Celery housekeeping).
    RoomStatus.AVAILABLE: frozenset(
        {RoomStatus.OCCUPIED, RoomStatus.CLEANING, RoomStatus.MAINTENANCE}
    ),
    RoomStatus.OCCUPIED: frozenset({RoomStatus.CLEANING}),
    RoomStatus.CLEANING: frozenset({RoomStatus.AVAILABLE, RoomStatus.MAINTENANCE}),
    RoomStatus.MAINTENANCE: frozenset({RoomStatus.AVAILABLE}),
}

BOOKING_TRANSITIONS: Final[dict[BookingStatus, frozenset[BookingStatus]]] = {
    BookingStatus.PENDING: frozenset(
        {BookingStatus.CONFIRMED, BookingStatus.CANCELLED, BookingStatus.EXPIRED}
    ),
    BookingStatus.CONFIRMED: frozenset(
        {BookingStatus.CHECKED_IN, BookingStatus.COMPLETED, BookingStatus.CANCELLED}
    ),
    BookingStatus.CHECKED_IN: frozenset({BookingStatus.COMPLETED}),
    BookingStatus.COMPLETED: frozenset(),
    BookingStatus.CANCELLED: frozenset(),
    BookingStatus.EXPIRED: frozenset(),
}


@dataclass(slots=True)
class Room:
    number: str
    name: str
    capacity: int
    price_per_block_cents: int
    status: RoomStatus = RoomStatus.AVAILABLE
    id: UUID = field(default_factory=uuid4)
    version: int = 1

    def transition_to(self, target: RoomStatus) -> None:
        if target not in ROOM_TRANSITIONS[self.status]:
            raise InvalidStateTransitionError(f"room {self.number}: {self.status} -> {target}")
        self.status = target


@dataclass(slots=True)
class Booking:
    room_id: UUID
    guest_name: str
    guest_email: str
    slot: TimeSlot
    total_price_cents: int
    status: BookingStatus = BookingStatus.PENDING
    hold_expires_at: datetime | None = None
    idempotency_key: str | None = None
    id: UUID = field(default_factory=uuid4)
    version: int = 1

    @property
    def is_blocking(self) -> bool:
        return self.status in BLOCKING_BOOKING_STATUSES

    def transition_to(self, target: BookingStatus) -> None:
        if target not in BOOKING_TRANSITIONS[self.status]:
            raise InvalidStateTransitionError(f"booking {self.id}: {self.status} -> {target}")
        self.status = target
        if target is not BookingStatus.PENDING:
            self.hold_expires_at = None
