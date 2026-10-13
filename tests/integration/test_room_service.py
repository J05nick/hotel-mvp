"""Staff action CLEANING -> AVAILABLE, alone and as the end of the booking lifecycle."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.application.booking_service import CreateHoldCommand
from app.domain.enums import BookingStatus, RoomStatus
from app.domain.events import RoomStatusEvent
from app.domain.exceptions import InvalidStateTransitionError, RoomNotFoundError
from app.domain.value_objects import BLOCK, TimeSlot
from app.infrastructure.db.models import BookingModel, RoomModel
from app.main import create_app
from app.presentation.api.deps import get_room_service
from tests.integration.conftest import Harness, Housekeeping, Rooms
from tests.support import T0

pytestmark = pytest.mark.integration


async def _status(h: Harness, room_id: UUID) -> RoomStatus:
    async with h.session_factory() as session:
        stmt = select(RoomModel.status).where(RoomModel.id == room_id)
        return (await session.execute(stmt)).scalar_one()


async def test_cleaning_room_becomes_available_and_announces_it(
    harness: Harness, rooms: Rooms, make_room: Callable[..., UUID]
) -> None:
    room_id = make_room(status=RoomStatus.CLEANING)

    room = await rooms.service.finish_cleaning(room_id)

    assert room.status is RoomStatus.AVAILABLE
    assert await _status(harness, room_id) is RoomStatus.AVAILABLE
    assert rooms.events.types() == ["room.status_changed"]
    event = rooms.events.events[0]
    assert isinstance(event, RoomStatusEvent)
    assert event.room_id == room_id
    assert (event.previous_status, event.status) == (RoomStatus.CLEANING, RoomStatus.AVAILABLE)


async def test_marking_an_available_room_clean_is_a_silent_no_op(
    rooms: Rooms, room_id: UUID
) -> None:
    first = await rooms.service.finish_cleaning(room_id)
    second = await rooms.service.finish_cleaning(room_id)

    assert first.status is second.status is RoomStatus.AVAILABLE
    assert rooms.events.events == []


@pytest.mark.parametrize("status", [RoomStatus.OCCUPIED, RoomStatus.MAINTENANCE])
async def test_rooms_that_are_not_being_cleaned_cannot_be_marked_clean(
    harness: Harness, rooms: Rooms, make_room: Callable[..., UUID], status: RoomStatus
) -> None:
    room_id = make_room(status=status)

    with pytest.raises(InvalidStateTransitionError):
        await rooms.service.finish_cleaning(room_id)

    assert await _status(harness, room_id) is status
    assert rooms.events.events == []


async def test_unknown_room(rooms: Rooms) -> None:
    with pytest.raises(RoomNotFoundError):
        await rooms.service.finish_cleaning(uuid.uuid4())


async def test_concurrent_requests_change_the_room_once_and_emit_one_event(
    rooms: Rooms, make_room: Callable[..., UUID]
) -> None:
    room_id = make_room(status=RoomStatus.CLEANING)

    results = await asyncio.gather(*(rooms.service.finish_cleaning(room_id) for _ in range(12)))

    assert {r.status for r in results} == {RoomStatus.AVAILABLE}
    assert rooms.events.types() == ["room.status_changed"]


async def test_full_lifecycle_booking_then_cleaning_then_available_again(
    harness: Harness, housekeeping: Housekeeping, rooms: Rooms, room_id: UUID
) -> None:
    """hold -> confirm -> stay ends -> CLEANING -> staff marks it clean -> bookable again."""
    slot = TimeSlot.from_blocks(T0, 2)
    cmd = CreateHoldCommand(room_id, slot, "Ada", "ada@example.com")
    booking = (await harness.service.create_hold(cmd)).booking
    await harness.service.confirm(booking.id)
    harness.clock.now = T0 + timedelta(hours=1, seconds=30)

    await housekeeping.service.complete_finished_bookings()
    assert await _status(harness, room_id) is RoomStatus.CLEANING

    await rooms.service.finish_cleaning(room_id)
    assert await _status(harness, room_id) is RoomStatus.AVAILABLE

    async with harness.session_factory() as session:
        row = (
            await session.execute(select(BookingModel.status).where(BookingModel.id == booking.id))
        ).scalar_one()
    assert row is BookingStatus.COMPLETED
    again = TimeSlot.from_blocks(T0 + BLOCK * 4, 2)
    assert (
        await harness.service.create_hold(CreateHoldCommand(room_id, again, "Bob", "b@x.com"))
    ).created


# ------------------------------------------------------------------------- HTTP
@pytest.fixture
async def client(rooms: Rooms) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_room_service] = lambda: rooms.service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def test_http_cleaned_endpoint(
    client: httpx.AsyncClient, make_room: Callable[..., UUID]
) -> None:
    room_id = make_room(status=RoomStatus.CLEANING)

    response = await client.post(f"/api/v1/rooms/{room_id}/cleaned")

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["status"]) == (str(room_id), "available")
    assert set(body) == {"id", "number", "name", "capacity", "price_per_block_cents", "status"}
    # Repeating the call is harmless.
    assert (await client.post(f"/api/v1/rooms/{room_id}/cleaned")).status_code == 200


async def test_http_errors(client: httpx.AsyncClient, make_room: Callable[..., UUID]) -> None:
    missing = await client.post(f"/api/v1/rooms/{uuid.uuid4()}/cleaned")
    assert (missing.status_code, missing.json()["error"]) == (404, "not_found")

    maintenance = make_room(status=RoomStatus.MAINTENANCE)
    conflict = await client.post(f"/api/v1/rooms/{maintenance}/cleaned")
    assert (conflict.status_code, conflict.json()["error"]) == (409, "invalid_state_transition")

    assert (await client.post("/api/v1/rooms/not-a-uuid/cleaned")).status_code == 422
