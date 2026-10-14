"""What PostgreSQL itself guarantees, independently of the application code."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from psycopg import errors as pg_errors
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError

from app.models import OVERLAP_CONSTRAINT, Booking, BookingStatus
from tests.conftest import T0

HALF = timedelta(minutes=30)


def _booking(
    room: uuid.UUID,
    start: datetime,
    end: datetime,
    status: BookingStatus = BookingStatus.CONFIRMED,
    hold: datetime | None = None,
) -> Booking:
    return Booking(
        room_id=room,
        guest_name="Guest",
        guest_email="g@example.com",
        during=Range(start, end, bounds="[)"),
        status=status,
        total_price_cents=1_000,
        hold_expires_at=hold,
    )


async def test_the_database_refuses_overlapping_active_bookings(
    app: FastAPI, room_id: uuid.UUID
) -> None:
    async with app.state.session_factory() as session:
        session.add(_booking(room_id, T0, T0 + 2 * HALF))
        await session.commit()

        session.add(_booking(room_id, T0 + HALF, T0 + 3 * HALF))
        with pytest.raises(IntegrityError) as caught:
            await session.commit()
        assert isinstance(caught.value.orig, pg_errors.ExclusionViolation)
        assert caught.value.orig.diag.constraint_name == OVERLAP_CONSTRAINT


@pytest.mark.parametrize(
    "released", [BookingStatus.CANCELLED, BookingStatus.EXPIRED, BookingStatus.COMPLETED]
)
async def test_released_statuses_do_not_block_the_range(
    app: FastAPI, room_id: uuid.UUID, released: BookingStatus
) -> None:
    async with app.state.session_factory() as session:
        session.add(_booking(room_id, T0, T0 + HALF, status=released))
        session.add(_booking(room_id, T0, T0 + HALF, status=BookingStatus.CONFIRMED))
        await session.commit()


async def test_concurrent_inserts_have_a_single_winner(app: FastAPI, room_id: uuid.UUID) -> None:
    async def attempt(i: int) -> bool:
        async with app.state.session_factory() as session:
            start = T0 + (HALF if i % 2 else timedelta(0))  # different but overlapping ranges
            session.add(_booking(room_id, start, start + 2 * HALF))
            try:
                await session.commit()
            except IntegrityError as exc:
                assert isinstance(exc.orig, pg_errors.ExclusionViolation)
                return False
            return True

    results = await asyncio.gather(*(attempt(i) for i in range(10)))

    assert results.count(True) == 1


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (T0 + timedelta(minutes=10), T0 + timedelta(minutes=40)),
        (T0, T0 + timedelta(minutes=45)),
        (T0, T0 + HALF + timedelta(milliseconds=1)),
    ],
    ids=["misaligned", "not-a-multiple", "sub-second"],
)
async def test_the_30_minute_grid_is_enforced_by_a_check(
    app: FastAPI, room_id: uuid.UUID, start: datetime, end: datetime
) -> None:
    async with app.state.session_factory() as session:
        session.add(_booking(room_id, start, end))
        with pytest.raises(IntegrityError) as caught:
            await session.commit()
        assert isinstance(caught.value.orig, pg_errors.CheckViolation)


async def test_a_hold_deadline_only_makes_sense_while_pending(
    app: FastAPI, room_id: uuid.UUID
) -> None:
    async with app.state.session_factory() as session:
        session.add(_booking(room_id, T0, T0 + HALF, hold=T0))  # confirmed WITH a deadline
        with pytest.raises(IntegrityError) as caught:
            await session.commit()
        assert isinstance(caught.value.orig, pg_errors.CheckViolation)
