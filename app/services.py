"""Business logic: holds, confirmations, cancellations, room cleaning, calendar, housekeeping.

How double bookings are prevented (demo edition, PostgreSQL only):
  * the EXCLUDE constraint on `bookings` is the single authority: whoever commits first owns the
    range, every overlapping INSERT fails with SQLSTATE 23P01, however many requests race;
  * a "hold" is just a `pending` booking with `hold_expires_at = now + 5 min`; it blocks the
    range like any active booking, and a lapsed hold is expired on the spot by the next request
    that needs the range (plus the periodic sweeper);
  * confirm / cancel take `SELECT ... FOR UPDATE` on the booking row so they serialise.

Events are published only after the transaction commits.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg import errors as pg_errors
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionFactory
from app.errors import ApiError, not_found
from app.events import EventHub
from app.models import (
    BLOCK_MINUTES,
    BLOCK_SECONDS,
    IN_PROGRESS,
    MAX_BLOCKS,
    VISIBLE,
    Booking,
    BookingStatus,
    Room,
    RoomStatus,
)
from app.schemas import CalendarBooking, CalendarResponse, CalendarRoom, RoomResponse

MAX_CALENDAR_WINDOW = timedelta(days=14)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _range(start: datetime, end: datetime) -> Range[datetime]:
    return Range(start, end, bounds="[)")


def _bounds(booking: Booking) -> tuple[datetime, datetime]:
    start, end = booking.during.lower, booking.during.upper
    assert start is not None and end is not None  # guaranteed by the CHECK constraint
    return start, end


# ------------------------------------------------------------------------------- validation
def validate_slot(start: datetime, end: datetime) -> None:
    """Whole 30-minute blocks on :00 / :30, tz-aware, 1..672 blocks."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ApiError(422, "invalid_time_slot", "start and end must be timezone-aware")
    if end <= start:
        raise ApiError(422, "invalid_time_slot", "end must be after start")
    if start.timestamp() % BLOCK_SECONDS or end.timestamp() % BLOCK_SECONDS:
        raise ApiError(
            422, "invalid_time_slot", f"start and end must be on {BLOCK_MINUTES}-minute boundaries"
        )
    if (end - start) > timedelta(minutes=BLOCK_MINUTES) * MAX_BLOCKS:
        raise ApiError(422, "invalid_time_slot", f"a booking cannot exceed {MAX_BLOCKS} blocks")


# ---------------------------------------------------------------------------------- holds
async def _find_replay(
    session: AsyncSession,
    key: str,
    room_id: UUID,
    start: datetime,
    end: datetime,
    email: str,
) -> Booking | None:
    existing = await session.scalar(select(Booking).where(Booking.idempotency_key == key))
    if existing is None:
        return None
    if (existing.room_id, _bounds(existing), existing.guest_email) != (
        room_id,
        (start, end),
        email,
    ):
        raise ApiError(422, "idempotency_key_reuse", "Idempotency-Key was used with other data")
    return existing


async def create_hold(
    session: AsyncSession,
    hub: EventHub,
    *,
    room_id: UUID,
    start: datetime,
    end: datetime,
    guest_name: str,
    guest_email: str,
    idempotency_key: str | None,
    ttl_seconds: int,
    now: datetime,
) -> tuple[Booking, bool]:
    """Hold a range for `ttl_seconds`. Returns (booking, created); created=False on a replay."""
    validate_slot(start, end)

    if idempotency_key:
        replay = await _find_replay(session, idempotency_key, room_id, start, end, guest_email)
        if replay:
            return replay, False

    room = await session.get(Room, room_id)
    if room is None:
        raise not_found("room")
    if room.status is RoomStatus.MAINTENANCE:
        raise ApiError(409, "room_not_bookable", f"room {room.number} is under maintenance")

    # A lapsed hold still blocks the range in the constraint until it is expired: do it now,
    # so nobody waits for the sweeper.
    lapsed = (
        await session.scalars(
            update(Booking)
            .where(
                Booking.room_id == room_id,
                Booking.status == BookingStatus.PENDING,
                Booking.hold_expires_at <= now,
                Booking.during.overlaps(_range(start, end)),
            )
            .values(status=BookingStatus.EXPIRED, hold_expires_at=None)
            .returning(Booking)
            .execution_options(synchronize_session=False)
        )
    ).all()

    booking = Booking(
        id=uuid4(),
        room_id=room_id,
        guest_name=guest_name,
        guest_email=guest_email,
        during=_range(start, end),
        status=BookingStatus.PENDING,
        total_price_cents=room.price_per_block_cents
        * int((end - start) / timedelta(minutes=BLOCK_MINUTES)),
        hold_expires_at=now + timedelta(seconds=ttl_seconds),
        idempotency_key=idempotency_key,
    )
    session.add(booking)
    try:
        await session.flush()  # the INSERT: here the exclusion constraint decides who wins
    except IntegrityError as exc:
        await session.rollback()
        # An identical retry that raced with its twin is a replay, not a conflict.
        if idempotency_key:
            replay = await _find_replay(session, idempotency_key, room_id, start, end, guest_email)
            if replay:
                return replay, False
        if isinstance(exc.orig, pg_errors.ExclusionViolation):
            raise ApiError(
                409, "slot_unavailable", "the room is already booked or held for that range"
            ) from exc
        raise
    await session.commit()

    for stale in lapsed:
        hub.booking(stale, now)
    hub.booking(booking, now)
    return booking, True


async def _locked_booking(session: AsyncSession, booking_id: UUID) -> Booking:
    # Pessimistic lock: concurrent confirm / cancel / sweeper of this row queue behind us.
    booking = await session.scalar(
        select(Booking)
        .where(Booking.id == booking_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if booking is None:
        raise not_found("booking")
    return booking


async def get_booking(session: AsyncSession, booking_id: UUID) -> Booking:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise not_found("booking")
    return booking


async def confirm_booking(
    session: AsyncSession, hub: EventHub, booking_id: UUID, now: datetime
) -> Booking:
    """pending -> confirmed. Idempotent. 410 if the hold lapsed."""
    booking = await _locked_booking(session, booking_id)
    if booking.status is BookingStatus.CONFIRMED:
        return booking
    if booking.status is not BookingStatus.PENDING:
        raise ApiError(
            409, "invalid_state_transition", f"cannot confirm a {booking.status} booking"
        )

    if booking.hold_expires_at is not None and booking.hold_expires_at <= now:
        booking.status = BookingStatus.EXPIRED
        booking.hold_expires_at = None
        await session.commit()
        hub.booking(booking, now)
        raise ApiError(410, "hold_expired", "the hold expired before confirmation")

    booking.status = BookingStatus.CONFIRMED
    booking.hold_expires_at = None
    await session.commit()
    hub.booking(booking, now)
    return booking


async def cancel_booking(
    session: AsyncSession, hub: EventHub, booking_id: UUID, now: datetime
) -> Booking:
    """pending | confirmed -> cancelled. Idempotent."""
    booking = await _locked_booking(session, booking_id)
    if booking.status is BookingStatus.CANCELLED:
        return booking
    if booking.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
        raise ApiError(409, "invalid_state_transition", f"cannot cancel a {booking.status} booking")

    booking.status = BookingStatus.CANCELLED
    booking.hold_expires_at = None
    await session.commit()
    hub.booking(booking, now)
    return booking


# ------------------------------------------------------------------------------------ rooms
async def finish_cleaning(
    session: AsyncSession, hub: EventHub, room_id: UUID, now: datetime
) -> Room:
    """cleaning -> available (staff action). Idempotent for an available room."""
    room = await session.scalar(
        select(Room)
        .where(Room.id == room_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if room is None:
        raise not_found("room")
    if room.status is RoomStatus.AVAILABLE:
        return room
    if room.status is not RoomStatus.CLEANING:
        # e.g. maintenance -> available is a different action: it must not hide behind this one.
        raise ApiError(
            409, "invalid_state_transition", "only a room in cleaning can be marked clean"
        )

    room.status = RoomStatus.AVAILABLE
    await session.commit()
    hub.room_status(room.id, room.status.value, RoomStatus.CLEANING.value, now)
    return room


# --------------------------------------------------------------------------------- calendar
async def calendar(
    session: AsyncSession, start: datetime, end: datetime, now: datetime
) -> CalendarResponse:
    """Every room with the bookings overlapping [start, end). Occupancy only."""
    if end <= start:
        raise ApiError(422, "invalid_window", "to must be after from")
    if end - start > MAX_CALENDAR_WINDOW:
        raise ApiError(
            422, "invalid_window", f"the window cannot exceed {MAX_CALENDAR_WINDOW.days} days"
        )

    rooms = (await session.scalars(select(Room).order_by(Room.number))).all()
    bookings = (
        await session.scalars(
            select(Booking)
            .where(
                Booking.during.overlaps(_range(start, end)),
                Booking.status.in_(VISIBLE),
                # a hold past its deadline no longer blocks anything
                (Booking.status != BookingStatus.PENDING) | (Booking.hold_expires_at > now),
            )
            .order_by(func.lower(Booking.during))
        )
    ).all()

    by_room: dict[UUID, list[CalendarBooking]] = {room.id: [] for room in rooms}
    for booking in bookings:
        b_start, b_end = _bounds(booking)
        by_room[booking.room_id].append(
            CalendarBooking(
                id=booking.id,
                status=booking.status,
                start=b_start,
                end=b_end,
                hold_expires_at=booking.hold_expires_at,
            )
        )
    return CalendarResponse(
        generated_at=now,
        start=start,
        end=end,
        rooms=[
            CalendarRoom(**RoomResponse.of(room).model_dump(), bookings=by_room[room.id])
            for room in rooms
        ],
    )


# ----------------------------------------------------------------------------- housekeeping
async def complete_finished_bookings(factory: SessionFactory, hub: EventHub, now: datetime) -> int:
    """Stays that ended -> completed, and their room -> cleaning. Returns bookings completed.

    One transaction per room. `SKIP LOCKED` means a row somebody else is busy with is simply
    picked up on the next sweep, and running the sweep twice changes nothing the second time.
    """
    async with factory() as session:
        room_ids = (
            await session.scalars(
                select(Booking.room_id)
                .where(Booking.status.in_(IN_PROGRESS), func.upper(Booking.during) <= now)
                .distinct()
            )
        ).all()

    completed = 0
    for room_id in room_ids:
        async with factory() as session:
            room = await session.scalar(
                select(Room).where(Room.id == room_id).with_for_update(skip_locked=True)
            )
            if room is None:
                continue
            finished = (
                await session.scalars(
                    select(Booking)
                    .where(
                        Booking.room_id == room_id,
                        Booking.status.in_(IN_PROGRESS),
                        func.upper(Booking.during) <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not finished:
                continue
            for booking in finished:
                booking.status = BookingStatus.COMPLETED

            # With back-to-back stays the next guest is already in: the room is not empty
            # and dirty yet, it turns "cleaning" when the last consecutive stay ends.
            in_use = await session.scalar(
                select(func.count()).where(
                    Booking.room_id == room_id,
                    Booking.status.in_(IN_PROGRESS),
                    Booking.during.contains(now),
                )
            )
            previous = room.status
            goes_dirty = previous in (RoomStatus.AVAILABLE, RoomStatus.OCCUPIED) and not in_use
            if goes_dirty:
                room.status = RoomStatus.CLEANING
            await session.commit()

        for booking in finished:
            hub.booking(booking, now)
        if goes_dirty:
            hub.room_status(room_id, RoomStatus.CLEANING.value, previous.value, now)
        completed += len(finished)
    return completed


async def expire_stale_holds(
    factory: SessionFactory, hub: EventHub, now: datetime, batch: int = 500
) -> int:
    """Pending bookings whose hold lapsed -> expired. Returns how many."""
    async with factory() as session:
        stale = (
            await session.scalars(
                select(Booking)
                .where(Booking.status == BookingStatus.PENDING, Booking.hold_expires_at <= now)
                .order_by(Booking.hold_expires_at)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for booking in stale:
            booking.status = BookingStatus.EXPIRED
            booking.hold_expires_at = None
        await session.commit()
    for booking in stale:
        hub.booking(booking, now)
    return len(stale)
