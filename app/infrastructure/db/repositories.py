"""SQLAlchemy adapters for the repository ports + Unit of Work.

LOCK ORDER (prevents deadlocks): always lock `rooms` rows BEFORE `bookings` rows
inside one transaction. Phase 3 (Celery) must follow the same order.

Database errors are translated to domain errors here, keyed on the stable
constraint names declared in the models/migration:
    23P01 exclusion_violation  ex_bookings_room_no_overlap -> SlotUnavailableError
    23505 unique_violation     uq_bookings_idempotency_key -> DuplicateIdempotencyKeyError
    55P03 lock_not_available / 57014 query_canceled        -> ResourceBusyError
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from psycopg import errors as pg_errors
from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.entities import Booking, Room
from app.domain.enums import BookingStatus
from app.domain.exceptions import (
    DuplicateIdempotencyKeyError,
    ResourceBusyError,
    SlotUnavailableError,
)
from app.domain.value_objects import TimeSlot
from app.infrastructure.db.models import BOOKING_OVERLAP_CONSTRAINT, BookingModel, RoomModel

_IDEMPOTENCY_CONSTRAINT = "uq_bookings_idempotency_key"
# A guest holds / has the room: confirmed but not yet completed.
_IN_PROGRESS_STATUSES = (BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN)


@contextmanager
def translate_db_errors() -> Iterator[None]:
    try:
        yield
    except IntegrityError as exc:
        orig = exc.orig
        constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
        if (
            isinstance(orig, pg_errors.ExclusionViolation)
            and constraint == BOOKING_OVERLAP_CONSTRAINT
        ):
            raise SlotUnavailableError(
                "the room is already booked for an overlapping range"
            ) from exc
        if isinstance(orig, pg_errors.UniqueViolation) and constraint == _IDEMPOTENCY_CONSTRAINT:
            raise DuplicateIdempotencyKeyError(_IDEMPOTENCY_CONSTRAINT) from exc
        raise
    except DBAPIError as exc:
        if isinstance(exc.orig, pg_errors.LockNotAvailable | pg_errors.QueryCanceled):
            raise ResourceBusyError("database is busy, retry shortly") from exc
        raise


def _room_to_entity(model: RoomModel) -> Room:
    return Room(
        id=model.id,
        number=model.number,
        name=model.name,
        capacity=model.capacity,
        price_per_block_cents=model.price_per_block_cents,
        status=model.status,
        version=model.version_id,
    )


def _booking_to_entity(model: BookingModel) -> Booking:
    during = model.during
    # CHECK constraint `during_bounded` guarantees both bounds exist.
    assert during.lower is not None
    assert during.upper is not None
    return Booking(
        id=model.id,
        room_id=model.room_id,
        guest_name=model.guest_name,
        guest_email=model.guest_email,
        slot=TimeSlot(during.lower, during.upper),
        total_price_cents=model.total_price_cents,
        status=model.status,
        hold_expires_at=model.hold_expires_at,
        idempotency_key=model.idempotency_key,
        version=model.version_id,
    )


class SqlAlchemyRoomRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, room_id: UUID) -> Room | None:
        with translate_db_errors():
            model = await self._session.get(RoomModel, room_id)
        return _room_to_entity(model) if model else None

    async def get_for_update(self, room_id: UUID, *, skip_locked: bool = False) -> Room | None:
        stmt = (
            select(RoomModel)
            .where(RoomModel.id == room_id)
            .with_for_update(skip_locked=skip_locked)
            .execution_options(populate_existing=True)
        )
        with translate_db_errors():
            model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _room_to_entity(model) if model else None

    async def save(self, room: Room) -> None:
        with translate_db_errors():
            model = await self._session.get(RoomModel, room.id)
            if model is None:  # pragma: no cover - callers load before saving
                raise LookupError(f"room {room.id} is not loaded in this unit of work")
            model.status = room.status
            # Optimistic locking: UPDATE ... WHERE version_id = :loaded bumps the version.
            await self._session.flush()
        room.version = model.version_id

    async def get_for_share(self, room_id: UUID) -> Room | None:
        stmt = select(RoomModel).where(RoomModel.id == room_id).with_for_update(read=True)
        with translate_db_errors():
            model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _room_to_entity(model) if model else None


class SqlAlchemyBookingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, booking: Booking) -> None:
        self._session.add(
            BookingModel(
                id=booking.id,
                room_id=booking.room_id,
                guest_name=booking.guest_name,
                guest_email=booking.guest_email,
                during=Range(booking.slot.start, booking.slot.end, bounds="[)"),
                status=booking.status,
                total_price_cents=booking.total_price_cents,
                hold_expires_at=booking.hold_expires_at,
                idempotency_key=booking.idempotency_key,
            )
        )
        # CONCURRENCY: the flush executes the INSERT. If a concurrent transaction
        # holds an overlapping range, PostgreSQL waits for it to finish and then
        # raises the exclusion violation (or accepts us if it rolled back).
        with translate_db_errors():
            await self._session.flush()

    async def get(self, booking_id: UUID) -> Booking | None:
        with translate_db_errors():
            model = await self._session.get(BookingModel, booking_id)
        return _booking_to_entity(model) if model else None

    async def get_for_update(self, booking_id: UUID) -> Booking | None:
        stmt = (
            select(BookingModel)
            .where(BookingModel.id == booking_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        with translate_db_errors():
            model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _booking_to_entity(model) if model else None

    async def get_by_idempotency_key(self, key: str) -> Booking | None:
        stmt = select(BookingModel).where(BookingModel.idempotency_key == key)
        with translate_db_errors():
            model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _booking_to_entity(model) if model else None

    async def save(self, booking: Booking) -> None:
        with translate_db_errors():
            model = await self._session.get(BookingModel, booking.id)
            if model is None:  # pragma: no cover - callers load before saving
                raise LookupError(f"booking {booking.id} is not loaded in this unit of work")
            model.status = booking.status
            model.hold_expires_at = booking.hold_expires_at
            # Optimistic locking: UPDATE ... WHERE version_id = :loaded bumps the version.
            await self._session.flush()
        booking.version = model.version_id

    async def rooms_with_finished_bookings(self, now: datetime, limit: int) -> list[UUID]:
        # Served by ix_bookings_status_end (status, upper(during)).
        stmt = (
            select(BookingModel.room_id)
            .where(
                BookingModel.status.in_(_IN_PROGRESS_STATUSES),
                func.upper(BookingModel.during) <= now,
            )
            .distinct()
            .limit(limit)
        )
        with translate_db_errors():
            return list((await self._session.execute(stmt)).scalars().all())

    async def list_finished_for_update(self, room_id: UUID, now: datetime) -> list[Booking]:
        stmt = (
            select(BookingModel)
            .where(
                BookingModel.room_id == room_id,
                BookingModel.status.in_(_IN_PROGRESS_STATUSES),
                func.upper(BookingModel.during) <= now,
            )
            .order_by(func.upper(BookingModel.during))
            # A booking being confirmed/cancelled by the API right now is simply picked
            # up by the next tick instead of making the whole sweep wait on it.
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        with translate_db_errors():
            rows = (await self._session.execute(stmt)).scalars().all()
        return [_booking_to_entity(row) for row in rows]

    async def has_active_booking(self, room_id: UUID, now: datetime) -> bool:
        stmt = select(
            exists().where(
                BookingModel.room_id == room_id,
                BookingModel.status.in_(_IN_PROGRESS_STATUSES),
                BookingModel.during.contains(now),
            )
        )
        with translate_db_errors():
            return bool((await self._session.execute(stmt)).scalar_one())

    async def list_expired_holds_for_update(self, now: datetime, limit: int) -> list[Booking]:
        # Served by the partial index ix_bookings_pending_hold_expires_at.
        stmt = (
            select(BookingModel)
            .where(
                BookingModel.status == BookingStatus.PENDING,
                BookingModel.hold_expires_at <= now,
            )
            .order_by(BookingModel.hold_expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        with translate_db_errors():
            rows = (await self._session.execute(stmt)).scalars().all()
        return [_booking_to_entity(row) for row in rows]

    async def expire_stale_holds(
        self, room_id: UUID, slot: TimeSlot, now: datetime
    ) -> list[Booking]:
        # A PENDING booking whose 5-minute hold lapsed still blocks the range in the
        # EXCLUDE constraint until it is expired. Doing it inline (instead of waiting for
        # the Celery sweep) frees the slot the instant the hold dies. Row locks taken
        # by this UPDATE serialise against concurrent confirm/cancel of the same row.
        stmt = (
            update(BookingModel)
            .where(
                BookingModel.room_id == room_id,
                BookingModel.status == BookingStatus.PENDING,
                BookingModel.hold_expires_at <= now,
                BookingModel.during.overlaps(Range(slot.start, slot.end, bounds="[)")),
            )
            .values(
                status=BookingStatus.EXPIRED,
                hold_expires_at=None,
                version_id=BookingModel.version_id + 1,
            )
            .returning(BookingModel)
            .execution_options(synchronize_session=False)
        )
        with translate_db_errors():
            rows = (await self._session.execute(stmt)).scalars().all()
        return [_booking_to_entity(row) for row in rows]


class SqlAlchemyUnitOfWork:
    """One transaction per use case. Anything not committed is rolled back on exit."""

    rooms: SqlAlchemyRoomRepository
    bookings: SqlAlchemyBookingRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        self.rooms = SqlAlchemyRoomRepository(self._session)
        self.bookings = SqlAlchemyBookingRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._session is not None
        try:
            await self._session.rollback()  # no-op after a successful commit
        finally:
            await self._session.close()
            self._session = None

    async def commit(self) -> None:
        assert self._session is not None
        with translate_db_errors():
            await self._session.commit()
