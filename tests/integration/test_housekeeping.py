"""Housekeeping sweeps against real PostgreSQL: bookings -> COMPLETED, rooms -> CLEANING."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, select, text

from app.application.booking_service import CreateHoldCommand
from app.domain.entities import Booking
from app.domain.enums import BookingStatus, RoomStatus
from app.domain.events import RoomStatusEvent
from app.domain.value_objects import BLOCK, TimeSlot
from app.infrastructure.db.models import BookingModel, RoomModel
from tests.integration.conftest import Harness, Housekeeping
from tests.support import T0

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _empty_bookings_table(engine: Engine) -> None:
    """The sweeps are global (as in production), so other tests' leftovers would count."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM bookings"))


async def _book(
    h: Harness, room_id: UUID, start_block: int, blocks: int = 2, *, confirm: bool = True
) -> Booking:
    result = await h.service.create_hold(
        CreateHoldCommand(
            room_id=room_id,
            slot=TimeSlot.from_blocks(T0 + BLOCK * start_block, blocks),
            guest_name="Ada",
            guest_email=f"g{start_block}@example.com",
        )
    )
    if confirm:
        await h.service.confirm(result.booking.id)
    return result.booking


async def _room_status(h: Harness, room_id: UUID) -> RoomStatus:
    async with h.session_factory() as session:
        return (
            await session.execute(select(RoomModel.status).where(RoomModel.id == room_id))
        ).scalar_one()


async def _booking_status(h: Harness, booking_id: UUID) -> BookingStatus:
    async with h.session_factory() as session:
        stmt = select(BookingModel.status).where(BookingModel.id == booking_id)
        return (await session.execute(stmt)).scalar_one()


def _end_of(block: int, blocks: int = 2) -> timedelta:
    """Offset from the fake clock's origin to just after a booking ends."""
    return (T0 + BLOCK * (block + blocks)) - T0 + timedelta(seconds=1)


# ------------------------------------------------------------ complete & clean
async def test_finished_booking_is_completed_and_room_goes_to_cleaning(
    harness: Harness, housekeeping: Housekeeping, room_id: UUID
) -> None:
    booking = await _book(harness, room_id, 0)
    harness.clock.now = T0 + _end_of(0)

    report = await housekeeping.service.complete_finished_bookings()

    assert (report.bookings_completed, report.rooms_set_cleaning, report.rooms_skipped) == (1, 1, 0)
    assert await _booking_status(harness, booking.id) is BookingStatus.COMPLETED
    assert await _room_status(harness, room_id) is RoomStatus.CLEANING
    assert housekeeping.events.types() == ["booking.completed", "room.status_changed"]
    room_event = housekeeping.events.events[1]
    assert isinstance(room_event, RoomStatusEvent)
    assert room_event.status is RoomStatus.CLEANING
    assert room_event.previous_status is RoomStatus.AVAILABLE


async def test_running_twice_changes_nothing_the_second_time(
    harness: Harness, housekeeping: Housekeeping, room_id: UUID
) -> None:
    await _book(harness, room_id, 0)
    harness.clock.now = T0 + _end_of(0)
    await housekeeping.service.complete_finished_bookings()
    housekeeping.events.events.clear()

    report = await housekeeping.service.complete_finished_bookings()

    assert report.bookings_completed == 0
    assert housekeeping.events.events == []


async def test_booking_in_progress_or_future_is_untouched(
    harness: Harness, housekeeping: Housekeeping, room_id: UUID
) -> None:
    running = await _book(harness, room_id, 0, blocks=2)  # 10:00-11:00
    future = await _book(harness, room_id, 6, blocks=2)  # 13:00-14:00
    harness.clock.now = T0 + timedelta(minutes=45)  # 10:45 => `running` still in progress

    report = await housekeeping.service.complete_finished_bookings()

    assert report.bookings_completed == 0
    assert await _booking_status(harness, running.id) is BookingStatus.CONFIRMED
    assert await _booking_status(harness, future.id) is BookingStatus.CONFIRMED
    assert await _room_status(harness, room_id) is RoomStatus.AVAILABLE


async def test_pending_and_cancelled_bookings_are_never_completed(
    harness: Harness, housekeeping: Housekeeping, room_id: UUID
) -> None:
    pending = await _book(harness, room_id, 0, confirm=False)
    cancelled = await _book(harness, room_id, 4)
    await harness.service.cancel(cancelled.id)
    harness.clock.now = T0 + timedelta(hours=1, seconds=1) - timedelta(minutes=0)  # pending ended

    await housekeeping.service.complete_finished_bookings()

    assert await _booking_status(harness, pending.id) is BookingStatus.PENDING
    assert await _booking_status(harness, cancelled.id) is BookingStatus.CANCELLED
    assert await _room_status(harness, room_id) is RoomStatus.AVAILABLE


async def test_several_finished_bookings_emit_a_single_room_event(
    harness: Harness, housekeeping: Housekeeping, room_id: UUID
) -> None:
    await _book(harness, room_id, 0)
    await _book(harness, room_id, 2)
    await _book(harness, room_id, 4)
    harness.clock.now = T0 + _end_of(4)

    report = await housekeeping.service.complete_finished_bookings()

    assert (report.bookings_completed, report.rooms_set_cleaning) == (3, 1)
    assert housekeeping.events.types().count("booking.completed") == 3
    assert housekeeping.events.types().count("room.status_changed") == 1


async def test_back_to_back_stay_keeps_the_room_in_use_until_the_last_one_ends(
    harness: Harness, housekeeping: Housekeeping, room_id: UUID
) -> None:
    first = await _book(harness, room_id, 0)  # 10:00-11:00
    second = await _book(harness, room_id, 2)  # 11:00-12:00
    harness.clock.now = T0 + timedelta(hours=1, seconds=30)  # 11:00:30

    await housekeeping.service.complete_finished_bookings()

    assert await _booking_status(harness, first.id) is BookingStatus.COMPLETED
    assert await _booking_status(harness, second.id) is BookingStatus.CONFIRMED
    assert await _room_status(harness, room_id) is RoomStatus.AVAILABLE  # next guest is in it
    assert "room.status_changed" not in housekeeping.events.types()

    harness.clock.now = T0 + timedelta(hours=2, seconds=30)
    await housekeeping.service.complete_finished_bookings()

    assert await _booking_status(harness, second.id) is BookingStatus.COMPLETED
    assert await _room_status(harness, room_id) is RoomStatus.CLEANING


@pytest.mark.parametrize("status", [RoomStatus.MAINTENANCE, RoomStatus.CLEANING])
async def test_rooms_in_maintenance_or_already_cleaning_keep_their_status(
    harness: Harness,
    housekeeping: Housekeeping,
    make_room: Callable[..., UUID],
    status: RoomStatus,
) -> None:
    room_id = make_room()
    booking = await _book(harness, room_id, 0)
    async with harness.session_factory() as session:  # staff changes the room afterwards
        room = await session.get(RoomModel, room_id)
        assert room is not None
        room.status = status
        await session.commit()
    harness.clock.now = T0 + _end_of(0)

    await housekeeping.service.complete_finished_bookings()

    assert await _booking_status(harness, booking.id) is BookingStatus.COMPLETED
    assert await _room_status(harness, room_id) is status
    assert housekeeping.events.types() == ["booking.completed"]


async def test_each_room_is_processed_independently(
    harness: Harness, housekeeping: Housekeeping, make_room: Callable[..., UUID]
) -> None:
    rooms = [make_room() for _ in range(4)]
    for room in rooms:
        await _book(harness, room, 0)
    harness.clock.now = T0 + _end_of(0)

    report = await housekeeping.service.complete_finished_bookings()

    assert (report.bookings_completed, report.rooms_set_cleaning) == (4, 4)
    for room in rooms:
        assert await _room_status(harness, room) is RoomStatus.CLEANING


async def test_max_rooms_limits_a_tick_and_the_next_tick_finishes_the_job(
    harness: Harness, housekeeping: Housekeeping, make_room: Callable[..., UUID]
) -> None:
    rooms = [make_room() for _ in range(5)]
    for room in rooms:
        await _book(harness, room, 0)
    harness.clock.now = T0 + _end_of(0)

    first = await housekeeping.service.complete_finished_bookings(max_rooms=2)
    second = await housekeeping.service.complete_finished_bookings(max_rooms=10)

    assert first.rooms_set_cleaning == 2
    assert second.rooms_set_cleaning == 3


# ------------------------------------------------------------------ concurrency
async def test_concurrent_workers_complete_every_booking_exactly_once(
    harness: Harness, housekeeping: Housekeeping, make_room: Callable[..., UUID]
) -> None:
    rooms = [make_room() for _ in range(6)]
    for room in rooms:
        await _book(harness, room, 0)
        await _book(harness, room, 2)
    harness.clock.now = T0 + _end_of(2)

    reports = await asyncio.gather(
        *(housekeeping.service.complete_finished_bookings() for _ in range(6))
    )

    assert sum(r.bookings_completed for r in reports) == 12
    assert sum(r.rooms_set_cleaning for r in reports) == 6
    types = housekeeping.events.types()
    assert types.count("booking.completed") == 12  # no duplicate events
    assert types.count("room.status_changed") == 6


async def test_room_locked_by_someone_else_is_skipped_then_picked_up(
    harness: Harness, housekeeping: Housekeeping, room_id: UUID
) -> None:
    booking = await _book(harness, room_id, 0)
    harness.clock.now = T0 + _end_of(0)

    async with harness.session_factory() as blocker:  # e.g. staff editing the room
        await blocker.execute(select(RoomModel).where(RoomModel.id == room_id).with_for_update())
        report = await housekeeping.service.complete_finished_bookings()
        assert (report.rooms_skipped, report.bookings_completed) == (1, 0)
        assert await _booking_status(harness, booking.id) is BookingStatus.CONFIRMED
        await blocker.rollback()

    report = await housekeeping.service.complete_finished_bookings()
    assert report.bookings_completed == 1
    assert await _room_status(harness, room_id) is RoomStatus.CLEANING


async def test_booking_locked_by_the_api_is_left_for_the_next_tick(
    harness: Harness, housekeeping: Housekeeping, room_id: UUID
) -> None:
    booking = await _book(harness, room_id, 0)
    harness.clock.now = T0 + _end_of(0)

    async with harness.session_factory() as api:  # e.g. a cancel request in flight
        await api.execute(
            select(BookingModel).where(BookingModel.id == booking.id).with_for_update()
        )
        report = await housekeeping.service.complete_finished_bookings()
        assert report.bookings_completed == 0
        await api.rollback()

    assert (await housekeeping.service.complete_finished_bookings()).bookings_completed == 1


# --------------------------------------------------------------- stale holds
async def test_expire_stale_holds_only_touches_lapsed_pending_bookings(
    harness: Harness, housekeeping: Housekeeping, room_id: UUID
) -> None:
    lapsed = await _book(harness, room_id, 0, confirm=False)
    confirmed = await _book(harness, room_id, 2)
    harness.clock.advance(seconds=200)
    fresh = await _book(harness, room_id, 4, confirm=False)  # hold ends at +500 s
    harness.clock.advance(seconds=101)  # lapsed: 301 s old; fresh: 101 s old

    expired = await housekeeping.service.expire_stale_holds()

    assert expired == 1
    assert await _booking_status(harness, lapsed.id) is BookingStatus.EXPIRED
    assert await _booking_status(harness, fresh.id) is BookingStatus.PENDING
    assert await _booking_status(harness, confirmed.id) is BookingStatus.CONFIRMED
    assert housekeeping.events.types() == ["booking.expired"]
    assert await housekeeping.service.expire_stale_holds() == 0  # idempotent


async def test_expired_slot_can_be_booked_again(
    harness: Harness, housekeeping: Housekeeping, room_id: UUID
) -> None:
    await _book(harness, room_id, 0, confirm=False)
    harness.clock.advance(seconds=301)
    await housekeeping.service.expire_stale_holds()
    await harness.redis.flushdb()

    again = await _book(harness, room_id, 0, confirm=False)

    assert again.status is BookingStatus.PENDING
