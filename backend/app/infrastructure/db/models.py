"""SQLAlchemy ORM models for Room and Booking.

Concurrency guarantees implemented at the database level
--------------------------------------------------------
1. `ex_bookings_room_no_overlap` — EXCLUDE USING gist (room_id WITH =, during WITH &&)
   PostgreSQL itself refuses two *active* bookings of the same room whose
   `tstzrange` intersect. This holds under ANY concurrency (multiple API
   replicas, Celery, manual SQL) because the check is performed atomically by
   the GiST index during INSERT/UPDATE — no application-level race can bypass
   it. Violations raise SQLSTATE 23P01 (exclusion_violation).
   Requires the `btree_gist` extension so a plain UUID can use `=` in GiST.

2. Partial predicate `WHERE status IN ('pending','confirmed','checked_in')`
   — cancelled / expired / completed bookings release their range instantly,
   without deleting history.

3. CHECK constraints replicate the domain's 30-minute block rules so that
   no writer can persist a malformed range.

4. `version_id` columns enable SQLAlchemy optimistic locking: an UPDATE whose
   `WHERE version_id = :old` matches 0 rows raises `StaleDataError`, which
   protects status transitions raced by the API and Celery Beat.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import BLOCKING_BOOKING_STATUSES, BookingStatus, RoomStatus
from app.domain.value_objects import BLOCK_MINUTES
from app.infrastructure.db.base import Base, TimestampMixin

_BLOCK_SECONDS: Final[int] = BLOCK_MINUTES * 60

# Single source of truth for the exclusion predicate (also used by the migration).
ACTIVE_BOOKING_PREDICATE: Final[str] = "status IN ({})".format(
    ", ".join(f"'{s.value}'" for s in sorted(BLOCKING_BOOKING_STATUSES))
)
BOOKING_OVERLAP_CONSTRAINT: Final[str] = "ex_bookings_room_no_overlap"


def _pg_enum(enum_cls: type[RoomStatus] | type[BookingStatus], name: str) -> SAEnum:
    # Persist the enum *values* ("available"), not member names ("AVAILABLE").
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


room_status_enum: Final[SAEnum] = _pg_enum(RoomStatus, "room_status")
booking_status_enum: Final[SAEnum] = _pg_enum(BookingStatus, "booking_status")


class RoomModel(TimestampMixin, Base):
    __tablename__ = "rooms"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    number: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    capacity: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Money as integer minor units (cents) — never floats.
    price_per_block_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RoomStatus] = mapped_column(
        room_status_enum,
        nullable=False,
        default=RoomStatus.AVAILABLE,
        server_default=RoomStatus.AVAILABLE.value,
    )
    # Optimistic concurrency token (see module docstring, point 4).
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    bookings: Mapped[list[BookingModel]] = relationship(back_populates="room", lazy="raise")

    __table_args__ = (
        CheckConstraint("capacity > 0", name="capacity_positive"),
        CheckConstraint("price_per_block_cents >= 0", name="price_non_negative"),
        Index("ix_rooms_status", "status"),
    )
    __mapper_args__: dict[str, Any] = {"version_id_col": version_id}  # noqa: RUF012

    def __repr__(self) -> str:
        return f"<Room {self.number} status={self.status}>"


class BookingModel(TimestampMixin, Base):
    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    room_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False
    )
    guest_name: Mapped[str] = mapped_column(String(120), nullable=False)
    guest_email: Mapped[str] = mapped_column(String(254), nullable=False)
    # Half-open range '[start, end)'. SQLAlchemy maps it to `Range[datetime]`.
    during: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)
    status: Mapped[BookingStatus] = mapped_column(
        booking_status_enum,
        nullable=False,
        default=BookingStatus.PENDING,
        server_default=BookingStatus.PENDING.value,
    )
    total_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    # Only meaningful while PENDING; Celery expires bookings past this instant.
    hold_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Client-supplied key so retried POSTs never create duplicate bookings.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    room: Mapped[RoomModel] = relationship(back_populates="bookings", lazy="raise")

    __table_args__ = (
        # ------------------------------------------------------------------
        # CONCURRENCY-CRITICAL: anti double-booking guarantee.
        # GiST index over (room_id, during); `=` on UUID needs btree_gist.
        # ------------------------------------------------------------------
        ExcludeConstraint(
            (room_id, "="),
            (during, "&&"),
            name=BOOKING_OVERLAP_CONSTRAINT,
            using="gist",
            where=text(ACTIVE_BOOKING_PREDICATE),
        ),
        # --- Range shape: bounded, non-empty, canonical half-open [) --------
        CheckConstraint(
            "NOT isempty(during) AND NOT lower_inf(during) AND NOT upper_inf(during)",
            name="during_bounded",
        ),
        CheckConstraint(
            "lower_inc(during) AND NOT upper_inc(during)",
            name="during_half_open",
        ),
        # --- 30-minute block alignment (epoch based, exact numeric math) ----
        # Alignment of both bounds + non-empty ⇒ duration is k * 30 min, k ≥ 1.
        CheckConstraint(
            f"mod(extract(epoch FROM lower(during)), {_BLOCK_SECONDS}) = 0 "
            f"AND mod(extract(epoch FROM upper(during)), {_BLOCK_SECONDS}) = 0",
            name="during_block_aligned",
        ),
        CheckConstraint("total_price_cents >= 0", name="total_price_non_negative"),
        CheckConstraint(
            "(status = 'pending') OR (hold_expires_at IS NULL)",
            name="hold_only_when_pending",
        ),
        # Calendar queries: "everything overlapping this window" across rooms.
        Index("ix_bookings_during", "during", postgresql_using="gist"),
        # Celery Beat scan: active bookings whose range already ended.
        Index("ix_bookings_status_end", "status", func.upper(during)),
        # Celery Beat scan: pending holds that timed out.
        Index(
            "ix_bookings_pending_hold_expires_at",
            "hold_expires_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_bookings_room_id", "room_id"),
    )
    __mapper_args__: dict[str, Any] = {"version_id_col": version_id}  # noqa: RUF012

    def __repr__(self) -> str:
        return f"<Booking {self.id} room={self.room_id} {self.during} {self.status}>"
