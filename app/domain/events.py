"""Domain events emitted after state changes.

Events are facts about something that already committed. They are delivered
best-effort over Redis pub/sub to WebSocket clients, so clients must treat the
REST snapshot as the source of truth and events as incremental hints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.enums import BookingStatus, RoomStatus


class EventType(StrEnum):
    BOOKING_HOLD_CREATED = "booking.hold_created"
    BOOKING_CONFIRMED = "booking.confirmed"
    BOOKING_CANCELLED = "booking.cancelled"
    BOOKING_EXPIRED = "booking.expired"
    ROOM_STATUS_CHANGED = "room.status_changed"


@dataclass(frozen=True, slots=True, kw_only=True)
class BookingEvent:
    type: EventType
    booking_id: UUID
    room_id: UUID
    status: BookingStatus
    start: datetime
    end: datetime
    occurred_at: datetime
    hold_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RoomStatusEvent:
    room_id: UUID
    status: RoomStatus
    previous_status: RoomStatus | None
    occurred_at: datetime
    type: EventType = field(default=EventType.ROOM_STATUS_CHANGED, init=False)


DomainEvent = BookingEvent | RoomStatusEvent
