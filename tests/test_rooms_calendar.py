"""Room cleaning and the calendar read model."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
import pytest

from app.models import RoomStatus
from tests.conftest import T0, FakeClock, body

MakeRoom = Callable[..., Awaitable[uuid.UUID]]
DAY = {"from": "2031-03-01T00:00:00Z", "to": "2031-03-02T00:00:00Z"}


# ----------------------------------------------------------------------------- cleaning
async def test_cleaning_room_becomes_available(
    client: httpx.AsyncClient, make_room: MakeRoom
) -> None:
    room = await make_room(status=RoomStatus.CLEANING)

    response = await client.post(f"/api/v1/rooms/{room}/cleaned")

    assert response.status_code == 200
    assert response.json()["status"] == "available"
    assert set(response.json()) == {
        "id", "number", "name", "capacity", "price_per_block_cents", "status"
    }  # fmt: skip


async def test_marking_an_available_room_clean_is_a_no_op(
    client: httpx.AsyncClient, room_id: uuid.UUID
) -> None:
    assert (await client.post(f"/api/v1/rooms/{room_id}/cleaned")).status_code == 200


@pytest.mark.parametrize("status", [RoomStatus.OCCUPIED, RoomStatus.MAINTENANCE])
async def test_only_a_room_in_cleaning_can_be_marked_clean(
    client: httpx.AsyncClient, make_room: MakeRoom, status: RoomStatus
) -> None:
    room = await make_room(status=status)
    response = await client.post(f"/api/v1/rooms/{room}/cleaned")
    assert (response.status_code, response.json()["error"]) == (409, "invalid_state_transition")


async def test_unknown_room_cleaning(client: httpx.AsyncClient) -> None:
    response = await client.post(f"/api/v1/rooms/{uuid.uuid4()}/cleaned")
    assert (response.status_code, response.json()["error"]) == (404, "not_found")


# ----------------------------------------------------------------------------- calendar
async def test_calendar_lists_rooms_and_visible_bookings(
    client: httpx.AsyncClient, room_id: uuid.UUID, make_room: MakeRoom
) -> None:
    cleaning = await make_room(status=RoomStatus.CLEANING)
    pending = (await client.post("/api/v1/bookings", json=body(room_id, 0))).json()
    confirmed = (await client.post("/api/v1/bookings", json=body(room_id, 120))).json()
    await client.post(f"/api/v1/bookings/{confirmed['id']}/confirm")

    response = await client.get("/api/v1/calendar", params=DAY)

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"generated_at", "start", "end", "rooms"}
    rooms = {r["id"]: r for r in data["rooms"]}
    assert [b["id"] for b in rooms[str(room_id)]["bookings"]] == [pending["id"], confirmed["id"]]
    assert [b["status"] for b in rooms[str(room_id)]["bookings"]] == ["pending", "confirmed"]
    assert rooms[str(cleaning)]["status"] == "cleaning"
    assert rooms[str(cleaning)]["bookings"] == []


async def test_calendar_hides_cancelled_expired_and_lapsed_holds(
    client: httpx.AsyncClient, room_id: uuid.UUID, clock: FakeClock
) -> None:
    cancelled = (await client.post("/api/v1/bookings", json=body(room_id, 0))).json()
    await client.post(f"/api/v1/bookings/{cancelled['id']}/cancel")
    await client.post("/api/v1/bookings", json=body(room_id, 120))  # will lapse
    clock.advance(seconds=301)  # nobody has swept it
    fresh = (await client.post("/api/v1/bookings", json=body(room_id, 240))).json()

    data = (await client.get("/api/v1/calendar", params=DAY)).json()

    bookings = next(r for r in data["rooms"] if r["id"] == str(room_id))["bookings"]
    assert [b["id"] for b in bookings] == [fresh["id"]]


async def test_calendar_window_is_half_open(client: httpx.AsyncClient, room_id: uuid.UUID) -> None:
    await client.post("/api/v1/bookings", json=body(room_id, 0, 60))  # 10:00-11:00
    inside = (await client.post("/api/v1/bookings", json=body(room_id, 60, 60))).json()

    window = {
        "from": (T0 + timedelta(hours=1)).isoformat(),
        "to": (T0 + timedelta(hours=2)).isoformat(),
    }
    data = (await client.get("/api/v1/calendar", params=window)).json()

    bookings = next(r for r in data["rooms"] if r["id"] == str(room_id))["bookings"]
    assert [b["id"] for b in bookings] == [inside["id"]]  # the 10:00-11:00 one ends AT the edge


async def test_calendar_never_exposes_guest_data(
    client: httpx.AsyncClient, room_id: uuid.UUID
) -> None:
    await client.post("/api/v1/bookings", json=body(room_id))
    text = (await client.get("/api/v1/calendar", params=DAY)).text
    assert "Ada" not in text
    assert "example.com" not in text


async def test_calendar_window_validation(client: httpx.AsyncClient) -> None:
    too_wide = await client.get(
        "/api/v1/calendar", params={"from": "2031-03-01T00:00:00Z", "to": "2031-04-01T00:00:00Z"}
    )
    assert (too_wide.status_code, too_wide.json()["error"]) == (422, "invalid_window")
    inverted = await client.get(
        "/api/v1/calendar", params={"from": "2031-03-02T00:00:00Z", "to": "2031-03-01T00:00:00Z"}
    )
    assert inverted.json()["error"] == "invalid_window"
    assert (await client.get("/api/v1/calendar")).status_code == 422  # missing params


async def test_health(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    assert (await client.get("/health/ready")).json() == {"status": "ok"}
