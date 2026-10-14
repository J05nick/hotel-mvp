"""The periodic sweeps: finished stays -> completed + room cleaning; lapsed holds -> expired."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app import services
from app.models import Booking, BookingStatus, Room, RoomStatus
from tests.conftest import T0, FakeClock, body

MakeRoom = Callable[..., Awaitable[uuid.UUID]]


async def _book(
    client: httpx.AsyncClient, room: uuid.UUID, start_min: int, minutes: int = 60
) -> str:
    created = await client.post("/api/v1/bookings", json=body(room, start_min, minutes))
    booking_id: str = created.json()["id"]
    await client.post(f"/api/v1/bookings/{booking_id}/confirm")
    return booking_id


async def _sweep(app: FastAPI, clock: FakeClock) -> int:
    return await services.complete_finished_bookings(
        app.state.session_factory, app.state.hub, clock()
    )


async def _room_status(app: FastAPI, room: uuid.UUID) -> RoomStatus:
    async with app.state.session_factory() as session:
        status: RoomStatus = (
            await session.execute(select(Room.status).where(Room.id == room))
        ).scalar_one()
        return status


async def _booking_status(app: FastAPI, booking_id: str) -> BookingStatus:
    async with app.state.session_factory() as session:
        stmt = select(Booking.status).where(Booking.id == uuid.UUID(booking_id))
        status: BookingStatus = (await session.execute(stmt)).scalar_one()
        return status


async def test_finished_stay_completes_and_the_room_goes_to_cleaning(
    app: FastAPI, client: httpx.AsyncClient, room_id: uuid.UUID, clock: FakeClock
) -> None:
    booking = await _book(client, room_id, 0)  # 10:00-11:00
    clock.now = T0 + timedelta(hours=1, seconds=1)

    assert await _sweep(app, clock) == 1

    assert await _booking_status(app, booking) is BookingStatus.COMPLETED
    assert await _room_status(app, room_id) is RoomStatus.CLEANING
    assert await _sweep(app, clock) == 0  # running twice changes nothing


async def test_stays_in_progress_or_in_the_future_are_left_alone(
    app: FastAPI, client: httpx.AsyncClient, room_id: uuid.UUID, clock: FakeClock
) -> None:
    running = await _book(client, room_id, 0)
    await _book(client, room_id, 240)
    clock.now = T0 + timedelta(minutes=45)

    assert await _sweep(app, clock) == 0
    assert await _booking_status(app, running) is BookingStatus.CONFIRMED
    assert await _room_status(app, room_id) is RoomStatus.AVAILABLE


async def test_pending_holds_are_never_completed(
    app: FastAPI, client: httpx.AsyncClient, room_id: uuid.UUID, clock: FakeClock
) -> None:
    hold = (await client.post("/api/v1/bookings", json=body(room_id))).json()["id"]
    clock.now = T0 + timedelta(hours=2)

    await _sweep(app, clock)

    assert await _booking_status(app, hold) is BookingStatus.PENDING
    assert await _room_status(app, room_id) is RoomStatus.AVAILABLE


async def test_back_to_back_stays_keep_the_room_in_use_until_the_last_ends(
    app: FastAPI, client: httpx.AsyncClient, room_id: uuid.UUID, clock: FakeClock
) -> None:
    first = await _book(client, room_id, 0)  # 10:00-11:00
    second = await _book(client, room_id, 60)  # 11:00-12:00
    clock.now = T0 + timedelta(hours=1, seconds=30)

    await _sweep(app, clock)

    assert await _booking_status(app, first) is BookingStatus.COMPLETED
    assert await _booking_status(app, second) is BookingStatus.CONFIRMED
    assert await _room_status(app, room_id) is RoomStatus.AVAILABLE  # next guest is inside

    clock.now = T0 + timedelta(hours=2, seconds=30)
    await _sweep(app, clock)
    assert await _room_status(app, room_id) is RoomStatus.CLEANING


async def test_maintenance_rooms_keep_their_status(
    app: FastAPI, client: httpx.AsyncClient, make_room: MakeRoom, clock: FakeClock
) -> None:
    room = await make_room()
    booking = await _book(client, room, 0)
    async with app.state.session_factory() as session:
        (await session.get(Room, room)).status = RoomStatus.MAINTENANCE
        await session.commit()
    clock.now = T0 + timedelta(hours=2)

    await _sweep(app, clock)

    assert await _booking_status(app, booking) is BookingStatus.COMPLETED
    assert await _room_status(app, room) is RoomStatus.MAINTENANCE


async def test_concurrent_sweeps_complete_each_booking_once(
    app: FastAPI, client: httpx.AsyncClient, make_room: MakeRoom, clock: FakeClock
) -> None:
    rooms = [await make_room() for _ in range(4)]
    for room in rooms:
        await _book(client, room, 0)
    clock.now = T0 + timedelta(hours=2)

    totals = await asyncio.gather(*(_sweep(app, clock) for _ in range(5)))

    assert sum(totals) == 4
    for room in rooms:
        assert await _room_status(app, room) is RoomStatus.CLEANING


async def test_full_lifecycle_ends_with_a_bookable_room(
    app: FastAPI, client: httpx.AsyncClient, room_id: uuid.UUID, clock: FakeClock
) -> None:
    await _book(client, room_id, 0)
    clock.now = T0 + timedelta(hours=1, seconds=1)
    await _sweep(app, clock)
    assert (await client.post(f"/api/v1/rooms/{room_id}/cleaned")).json()["status"] == "available"

    assert (await client.post("/api/v1/bookings", json=body(room_id, 240))).status_code == 201


async def test_expire_stale_holds_only_touches_lapsed_pending_bookings(
    app: FastAPI, client: httpx.AsyncClient, room_id: uuid.UUID, clock: FakeClock
) -> None:
    lapsed = (await client.post("/api/v1/bookings", json=body(room_id, 0))).json()["id"]
    confirmed = await _book(client, room_id, 120)
    clock.advance(seconds=200)
    fresh = (await client.post("/api/v1/bookings", json=body(room_id, 240))).json()["id"]
    clock.advance(seconds=101)

    expired = await services.expire_stale_holds(app.state.session_factory, app.state.hub, clock())

    assert expired == 1
    assert await _booking_status(app, lapsed) is BookingStatus.EXPIRED
    assert await _booking_status(app, fresh) is BookingStatus.PENDING
    assert await _booking_status(app, confirmed) is BookingStatus.CONFIRMED
