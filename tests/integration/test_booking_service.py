"""BookingService against real PostgreSQL + Redis."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.application.booking_service import CreateHoldCommand
from app.domain.enums import BookingStatus, RoomStatus
from app.domain.exceptions import (
    BookingNotFoundError,
    HoldExpiredError,
    IdempotencyKeyReuseError,
    InvalidStateTransitionError,
    RoomNotBookableError,
    RoomNotFoundError,
    SlotUnavailableError,
)
from app.domain.value_objects import BLOCK, TimeSlot
from app.infrastructure.cache.slot_holds import block_keys
from app.infrastructure.db.models import BookingModel
from tests.integration.conftest import Harness
from tests.support import T0, AlwaysAcquireHoldStore

pytestmark = pytest.mark.integration


def _cmd(
    room_id: UUID,
    start_block: int = 0,
    blocks: int = 2,
    guest: str = "ada@example.com",
    key: str | None = None,
) -> CreateHoldCommand:
    return CreateHoldCommand(
        room_id=room_id,
        slot=TimeSlot.from_blocks(T0 + BLOCK * start_block, blocks),
        guest_name="Ada",
        guest_email=guest,
        idempotency_key=key,
    )


async def _count(h: Harness, room_id: UUID, *statuses: BookingStatus) -> int:
    async with h.session_factory() as session:
        stmt = select(func.count()).select_from(BookingModel).where(BookingModel.room_id == room_id)
        if statuses:
            stmt = stmt.where(BookingModel.status.in_(statuses))
        return (await session.execute(stmt)).scalar_one()


# ------------------------------------------------------------------ happy path
async def test_hold_then_confirm(harness: Harness, room_id: UUID) -> None:
    result = await harness.service.create_hold(_cmd(room_id, blocks=3))
    booking = result.booking

    assert result.created
    assert booking.status is BookingStatus.PENDING
    assert booking.total_price_cents == 3_000  # 3 blocks x 1000
    assert booking.hold_expires_at == harness.clock() + timedelta(seconds=300)
    keys = block_keys(room_id, booking.slot)
    assert [await harness.redis.get(k) for k in keys] == [str(booking.id)] * 3
    for key in keys:
        assert 0 < await harness.redis.ttl(key) <= 300

    confirmed = await harness.service.confirm(booking.id)

    assert confirmed.status is BookingStatus.CONFIRMED
    assert confirmed.hold_expires_at is None
    assert await harness.redis.keys("hold:*") == []  # cart released, DB now guards the slot
    assert harness.events.types() == ["booking.hold_created", "booking.confirmed"]


async def test_confirm_is_idempotent(harness: Harness, room_id: UUID) -> None:
    booking = (await harness.service.create_hold(_cmd(room_id))).booking
    await harness.service.confirm(booking.id)
    again = await harness.service.confirm(booking.id)
    assert again.status is BookingStatus.CONFIRMED
    assert harness.events.types().count("booking.confirmed") == 1


async def test_back_to_back_ranges_do_not_conflict(harness: Harness, room_id: UUID) -> None:
    await harness.service.create_hold(_cmd(room_id, start_block=0, blocks=2))
    await harness.service.create_hold(_cmd(room_id, start_block=2, blocks=2, guest="b@example.com"))
    assert await _count(harness, room_id) == 2


# ----------------------------------------------------------------- concurrency
async def test_concurrent_holds_same_range_only_one_wins(harness: Harness, room_id: UUID) -> None:
    attempts = 30
    outcomes = await asyncio.gather(
        *(
            harness.service.create_hold(_cmd(room_id, guest=f"g{i}@example.com"))
            for i in range(attempts)
        ),
        return_exceptions=True,
    )

    winners = [o for o in outcomes if not isinstance(o, BaseException)]
    losers = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(winners) == 1
    assert all(isinstance(e, SlotUnavailableError) for e in losers), losers
    assert await _count(harness, room_id, BookingStatus.PENDING) == 1
    # Redis ended up holding exactly the winner's blocks (losers rolled theirs back).
    owners = {await harness.redis.get(k) async for k in harness.redis.scan_iter("hold:*")}
    assert owners == {str(winners[0].booking.id)}


async def test_database_constraint_is_last_line_of_defence(
    make_harness: Callable[..., Harness], room_id: UUID
) -> None:
    """With Redis effectively disabled every request reaches PostgreSQL; still only one wins."""
    h = make_harness(holds=AlwaysAcquireHoldStore())

    outcomes = await asyncio.gather(
        *(h.service.create_hold(_cmd(room_id, guest=f"g{i}@example.com")) for i in range(20)),
        return_exceptions=True,
    )

    assert sum(not isinstance(o, BaseException) for o in outcomes) == 1
    assert all(
        isinstance(o, SlotUnavailableError) for o in outcomes if isinstance(o, BaseException)
    )
    assert await _count(h, room_id) == 1
    assert h.events.types() == ["booking.hold_created"]  # losers published nothing


async def test_overlapping_partial_range_is_rejected_without_leaking_redis_keys(
    harness: Harness, room_id: UUID
) -> None:
    held = (await harness.service.create_hold(_cmd(room_id, start_block=2, blocks=1))).booking

    with pytest.raises(SlotUnavailableError):
        await harness.service.create_hold(_cmd(room_id, start_block=0, blocks=4, guest="b@x.com"))

    remaining = [k async for k in harness.redis.scan_iter("hold:*")]
    assert remaining == block_keys(room_id, held.slot)


async def test_concurrent_confirm_and_cancel_end_in_one_consistent_state(
    harness: Harness, room_id: UUID
) -> None:
    booking = (await harness.service.create_hold(_cmd(room_id))).booking

    outcomes = await asyncio.gather(
        harness.service.confirm(booking.id),
        harness.service.cancel(booking.id),
        return_exceptions=True,
    )

    final = await harness.service.get(booking.id)
    assert final.status in {BookingStatus.CONFIRMED, BookingStatus.CANCELLED}
    # Pessimistic FOR UPDATE serialises them: whichever ran second either saw the first's
    # result (cancel after confirm is legal) or failed with an invalid transition.
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException) or isinstance(
            outcome, InvalidStateTransitionError
        )


# -------------------------------------------------------------------- expiry
async def test_confirm_after_hold_expired_raises_and_marks_expired(
    harness: Harness, room_id: UUID
) -> None:
    booking = (await harness.service.create_hold(_cmd(room_id))).booking
    harness.clock.advance(seconds=301)

    with pytest.raises(HoldExpiredError):
        await harness.service.confirm(booking.id)

    assert (await harness.service.get(booking.id)).status is BookingStatus.EXPIRED
    assert harness.events.types() == ["booking.hold_created", "booking.expired"]
    assert await harness.redis.keys("hold:*") == []


async def test_stale_pending_hold_is_expired_inline_so_slot_frees_immediately(
    harness: Harness, room_id: UUID
) -> None:
    first = (await harness.service.create_hold(_cmd(room_id))).booking
    harness.clock.advance(seconds=301)
    await harness.redis.flushdb()  # Redis TTL fired; Celery's sweep has NOT run yet

    second = (await harness.service.create_hold(_cmd(room_id, guest="b@example.com"))).booking

    assert (await harness.service.get(first.id)).status is BookingStatus.EXPIRED
    assert second.status is BookingStatus.PENDING
    assert harness.events.types() == [
        "booking.hold_created",
        "booking.expired",  # emitted for `first`
        "booking.hold_created",  # emitted for `second`
    ]
    with pytest.raises(InvalidStateTransitionError):  # EXPIRED is terminal
        await harness.service.confirm(first.id)


async def test_unexpired_pending_hold_is_not_stolen(harness: Harness, room_id: UUID) -> None:
    await harness.service.create_hold(_cmd(room_id))
    harness.clock.advance(seconds=299)
    await harness.redis.flushdb()  # even with Redis empty, the DB row still blocks the range

    with pytest.raises(SlotUnavailableError):
        await harness.service.create_hold(_cmd(room_id, guest="b@example.com"))


# -------------------------------------------------------------------- cancel
async def test_cancel_frees_range_and_cart(harness: Harness, room_id: UUID) -> None:
    booking = (await harness.service.create_hold(_cmd(room_id))).booking

    cancelled = await harness.service.cancel(booking.id)

    assert cancelled.status is BookingStatus.CANCELLED
    assert await harness.redis.keys("hold:*") == []
    assert (
        await harness.service.cancel(booking.id)
    ).status is BookingStatus.CANCELLED  # idempotent
    again = await harness.service.create_hold(_cmd(room_id, guest="b@example.com"))
    assert again.created
    assert harness.events.types().count("booking.cancelled") == 1


async def test_cannot_cancel_after_completion(harness: Harness, room_id: UUID) -> None:
    booking = (await harness.service.create_hold(_cmd(room_id))).booking
    await harness.service.confirm(booking.id)
    async with harness.session_factory() as session:
        row = await session.get(BookingModel, booking.id)
        assert row is not None
        row.status = BookingStatus.COMPLETED
        await session.commit()

    with pytest.raises(InvalidStateTransitionError):
        await harness.service.cancel(booking.id)


# --------------------------------------------------------------- idempotency
async def test_idempotency_key_replays_the_same_booking(harness: Harness, room_id: UUID) -> None:
    first = await harness.service.create_hold(_cmd(room_id, key="key-0001-abc"))
    replay = await harness.service.create_hold(_cmd(room_id, key="key-0001-abc"))

    assert first.created
    assert not replay.created
    assert replay.booking.id == first.booking.id
    assert await _count(harness, room_id) == 1
    assert harness.events.types() == ["booking.hold_created"]


async def test_idempotency_key_with_different_params_is_rejected(
    harness: Harness, room_id: UUID
) -> None:
    await harness.service.create_hold(_cmd(room_id, key="key-0002-abc"))
    with pytest.raises(IdempotencyKeyReuseError):
        await harness.service.create_hold(_cmd(room_id, start_block=4, key="key-0002-abc"))


async def test_concurrent_identical_requests_create_a_single_booking(
    harness: Harness, room_id: UUID
) -> None:
    outcomes = await asyncio.gather(
        *(harness.service.create_hold(_cmd(room_id, key="key-0003-abc")) for _ in range(15)),
        return_exceptions=True,
    )

    ids = {o.booking.id for o in outcomes if not isinstance(o, BaseException)}
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(ids) == 1
    assert all(isinstance(e, SlotUnavailableError) for e in errors), errors  # in-flight retries
    assert await _count(harness, room_id) == 1


# ---------------------------------------------------------------- validation
async def test_unknown_room_is_rejected_before_touching_redis(harness: Harness) -> None:
    import uuid

    with pytest.raises(RoomNotFoundError):
        await harness.service.create_hold(_cmd(uuid.uuid4()))
    assert await harness.redis.keys("hold:*") == []


async def test_room_in_maintenance_is_not_bookable(
    harness: Harness, make_room: Callable[..., UUID]
) -> None:
    room = make_room(status=RoomStatus.MAINTENANCE)
    with pytest.raises(RoomNotBookableError):
        await harness.service.create_hold(_cmd(room))
    assert await harness.redis.keys("hold:*") == []


async def test_missing_booking(harness: Harness) -> None:
    import uuid

    for call in (harness.service.get, harness.service.confirm, harness.service.cancel):
        with pytest.raises(BookingNotFoundError):
            await call(uuid.uuid4())
