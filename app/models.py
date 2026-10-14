"""Database schema.

The star of the show is `ex_bookings_room_no_overlap`: PostgreSQL itself refuses two active
bookings of the same room whose time ranges overlap, no matter how many requests race.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

BLOCK_MINUTES = 30
BLOCK_SECONDS = BLOCK_MINUTES * 60
MAX_BLOCKS = 48 * 14  # a booking may not exceed two weeks


class RoomStatus(StrEnum):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    CLEANING = "cleaning"
    MAINTENANCE = "maintenance"


class BookingStatus(StrEnum):
    PENDING = "pending"  # a hold: blocks the range until `hold_expires_at`
    CONFIRMED = "confirmed"
    CHECKED_IN = "checked_in"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# Statuses that occupy time. Must match the WHERE of the exclusion constraint below.
ACTIVE = (BookingStatus.PENDING, BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN)
# A stay in progress (confirmed or checked in), as opposed to a mere hold.
IN_PROGRESS = (BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN)
# Shown on the calendar; cancelled and expired bookings release their range.
VISIBLE = (*ACTIVE, BookingStatus.COMPLETED)

OVERLAP_CONSTRAINT = "ex_bookings_room_no_overlap"
IDEMPOTENCY_CONSTRAINT = "uq_bookings_idempotency_key"


def _enum(cls: type[StrEnum], name: str) -> SAEnum:
    return SAEnum(cls, name=name, values_callable=lambda members: [m.value for m in members])


class Base(DeclarativeBase):
    # Every `datetime` column is TIMESTAMPTZ: naive timestamps are never stored.
    type_annotation_map: ClassVar[dict[type, DateTime]] = {datetime: DateTime(timezone=True)}


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    number: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    capacity: Mapped[int] = mapped_column(SmallInteger)
    price_per_block_cents: Mapped[int]
    status: Mapped[RoomStatus] = mapped_column(
        _enum(RoomStatus, "room_status"), default=RoomStatus.AVAILABLE
    )

    __table_args__ = (
        CheckConstraint("capacity > 0", name="ck_rooms_capacity_positive"),
        CheckConstraint("price_per_block_cents >= 0", name="ck_rooms_price_non_negative"),
    )


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rooms.id", ondelete="RESTRICT"))
    guest_name: Mapped[str] = mapped_column(String(120))
    guest_email: Mapped[str] = mapped_column(String(254))
    # Half-open range '[start, end)', so 10:00-10:30 and 10:30-11:00 do not overlap.
    during: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)
    status: Mapped[BookingStatus] = mapped_column(
        _enum(BookingStatus, "booking_status"), default=BookingStatus.PENDING
    )
    total_price_cents: Mapped[int]
    hold_expires_at: Mapped[datetime | None] = mapped_column(default=None)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        # No two active bookings of one room may overlap. `=` on a UUID inside a GiST index
        # needs the btree_gist extension (created in db.init_db).
        ExcludeConstraint(
            ("room_id", "="),
            ("during", "&&"),
            name=OVERLAP_CONSTRAINT,
            using="gist",
            where=text("status IN ('pending', 'confirmed', 'checked_in')"),
        ),
        # Defence in depth: the 30-minute grid is enforced by the database too.
        CheckConstraint(
            "NOT isempty(during) AND NOT lower_inf(during) AND NOT upper_inf(during)"
            " AND lower_inc(during) AND NOT upper_inc(during)",
            name="ck_bookings_during_bounded_half_open",
        ),
        CheckConstraint(
            f"mod(extract(epoch FROM lower(during)), {BLOCK_SECONDS}) = 0"
            f" AND mod(extract(epoch FROM upper(during)), {BLOCK_SECONDS}) = 0",
            name="ck_bookings_during_block_aligned",
        ),
        CheckConstraint(
            "(status = 'pending') OR (hold_expires_at IS NULL)",
            name="ck_bookings_hold_only_when_pending",
        ),
        CheckConstraint("total_price_cents >= 0", name="ck_bookings_price_non_negative"),
        Index(IDEMPOTENCY_CONSTRAINT, "idempotency_key", unique=True),
        Index("ix_bookings_during", "during", postgresql_using="gist"),
    )
