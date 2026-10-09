"""Domain enumerations (persisted as native PostgreSQL ENUM types)."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class RoomStatus(StrEnum):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    CLEANING = "cleaning"
    MAINTENANCE = "maintenance"


class BookingStatus(StrEnum):
    PENDING = "pending"  # created from a Redis hold, awaiting confirmation/payment
    CONFIRMED = "confirmed"  # paid / guaranteed
    CHECKED_IN = "checked_in"  # guest is in the room
    COMPLETED = "completed"  # time range ended; room sent to cleaning
    CANCELLED = "cancelled"  # cancelled by guest or staff
    EXPIRED = "expired"  # pending booking never confirmed


# Statuses that physically occupy the time range. Only these participate in the
# PostgreSQL EXCLUDE constraint; terminal statuses release the slot.
# NOTE: must stay in sync with the `WHERE` predicate of `ex_bookings_room_no_overlap`.
BLOCKING_BOOKING_STATUSES: Final[frozenset[BookingStatus]] = frozenset(
    {BookingStatus.PENDING, BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN}
)
