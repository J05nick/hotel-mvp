from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.entities import Booking, Room
from app.domain.enums import BookingStatus, RoomStatus
from app.domain.exceptions import InvalidStateTransitionError
from app.domain.value_objects import TimeSlot


def _booking() -> Booking:
    start = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
    return Booking(
        room_id=uuid4(),
        guest_name="Ada",
        guest_email="ada@example.com",
        slot=TimeSlot(start, start + timedelta(hours=1)),
        total_price_cents=5_000,
        hold_expires_at=start,
    )


def test_room_lifecycle() -> None:
    room = Room(number="101", name="Suite", capacity=2, price_per_block_cents=2_500)
    room.transition_to(RoomStatus.OCCUPIED)
    room.transition_to(RoomStatus.CLEANING)
    room.transition_to(RoomStatus.AVAILABLE)
    with pytest.raises(InvalidStateTransitionError):
        room.transition_to(RoomStatus.CLEANING)


def test_booking_confirm_clears_hold() -> None:
    booking = _booking()
    assert booking.is_blocking
    booking.transition_to(BookingStatus.CONFIRMED)
    assert booking.hold_expires_at is None


def test_terminal_booking_releases_slot() -> None:
    booking = _booking()
    booking.transition_to(BookingStatus.CANCELLED)
    assert not booking.is_blocking
    with pytest.raises(InvalidStateTransitionError):
        booking.transition_to(BookingStatus.CONFIRMED)
