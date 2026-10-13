"""Builders for domain events, shared by every use case."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from app.domain.entities import Booking
from app.domain.enums import BookingStatus
from app.domain.events import BookingEvent, EventType

_EVENT_FOR_STATUS: Final[dict[BookingStatus, EventType]] = {
    BookingStatus.PENDING: EventType.BOOKING_HOLD_CREATED,
    BookingStatus.CONFIRMED: EventType.BOOKING_CONFIRMED,
    BookingStatus.CANCELLED: EventType.BOOKING_CANCELLED,
    BookingStatus.COMPLETED: EventType.BOOKING_COMPLETED,
    BookingStatus.EXPIRED: EventType.BOOKING_EXPIRED,
}


def booking_event(booking: Booking, now: datetime) -> BookingEvent:
    """Event describing the booking's CURRENT status (call after the transition)."""
    return BookingEvent(
        type=_EVENT_FOR_STATUS[booking.status],
        booking_id=booking.id,
        room_id=booking.room_id,
        status=booking.status,
        start=booking.slot.start,
        end=booking.slot.end,
        occurred_at=now,
        hold_expires_at=booking.hold_expires_at,
    )
