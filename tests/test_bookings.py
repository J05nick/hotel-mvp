"""The booking flow over HTTP, against a real PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
import pytest

from app.models import RoomStatus
from tests.conftest import FakeClock, body

Room = uuid.UUID
MakeRoom = Callable[..., Awaitable[uuid.UUID]]


async def test_hold_then_confirm(
    client: httpx.AsyncClient, room_id: Room, clock: FakeClock
) -> None:
    created = await client.post("/api/v1/bookings", json=body(room_id))

    assert created.status_code == 201
    booking = created.json()
    assert booking["status"] == "pending"
    assert booking["blocks"] == 2
    assert booking["total_price_cents"] == 2_000
    assert booking["hold_expires_at"] == (clock() + timedelta(seconds=300)).isoformat().replace(
        "+00:00", "Z"
    )

    confirmed = await client.post(f"/api/v1/bookings/{booking['id']}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["hold_expires_at"] is None
    assert (await client.get(f"/api/v1/bookings/{booking['id']}")).json()["status"] == "confirmed"


async def test_overlapping_range_is_refused(client: httpx.AsyncClient, room_id: Room) -> None:
    assert (await client.post("/api/v1/bookings", json=body(room_id))).status_code == 201

    clash = await client.post("/api/v1/bookings", json=body(room_id, start_min=30))

    assert clash.status_code == 409
    assert clash.json()["error"] == "slot_unavailable"


async def test_back_to_back_and_other_rooms_are_fine(
    client: httpx.AsyncClient, room_id: Room, make_room: MakeRoom
) -> None:
    other = await make_room()
    assert (await client.post("/api/v1/bookings", json=body(room_id, 0, 60))).status_code == 201
    assert (await client.post("/api/v1/bookings", json=body(room_id, 60, 60))).status_code == 201
    assert (await client.post("/api/v1/bookings", json=body(other, 0, 120))).status_code == 201


async def test_concurrent_requests_for_one_slot_have_a_single_winner(
    client: httpx.AsyncClient, room_id: Room
) -> None:
    """The headline guarantee: 20 simultaneous requests, exactly one booking."""
    responses = await asyncio.gather(
        *(
            client.post("/api/v1/bookings", json=body(room_id, guest_email=f"g{i}@example.com"))
            for i in range(20)
        )
    )

    codes = sorted(r.status_code for r in responses)
    assert codes == [201] + [409] * 19
    assert {r.json()["error"] for r in responses if r.status_code == 409} == {"slot_unavailable"}


@pytest.mark.parametrize(
    ("start_min", "minutes"),
    [(15, 30), (0, 45), (0, 0), (0, -30)],
    ids=["misaligned-start", "partial-block", "empty", "inverted"],
)
async def test_invalid_time_slot(
    client: httpx.AsyncClient, room_id: Room, start_min: int, minutes: int
) -> None:
    response = await client.post("/api/v1/bookings", json=body(room_id, start_min, minutes))
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_time_slot"


async def test_naive_datetime_and_bad_email_fail_validation(
    client: httpx.AsyncClient, room_id: Room
) -> None:
    naive = body(room_id) | {"start": "2031-03-01T10:00:00", "end": "2031-03-01T11:00:00"}
    assert (await client.post("/api/v1/bookings", json=naive)).status_code == 422
    assert (
        await client.post("/api/v1/bookings", json=body(room_id, guest_email="nope"))
    ).status_code == 422


async def test_unknown_room_and_unknown_booking(client: httpx.AsyncClient) -> None:
    ghost = uuid.uuid4()
    assert (await client.post("/api/v1/bookings", json=body(ghost))).status_code == 404
    for call in (
        client.get(f"/api/v1/bookings/{ghost}"),
        client.post(f"/api/v1/bookings/{ghost}/confirm"),
        client.post(f"/api/v1/bookings/{ghost}/cancel"),
    ):
        assert (await call).json()["error"] == "not_found"


async def test_room_in_maintenance_is_not_bookable(
    client: httpx.AsyncClient, make_room: MakeRoom
) -> None:
    room = await make_room(status=RoomStatus.MAINTENANCE)
    response = await client.post("/api/v1/bookings", json=body(room))
    assert (response.status_code, response.json()["error"]) == (409, "room_not_bookable")


# ---------------------------------------------------------------------------------- holds
async def test_confirming_after_the_hold_lapsed_gives_410_and_frees_the_slot(
    client: httpx.AsyncClient, room_id: Room, clock: FakeClock
) -> None:
    booking = (await client.post("/api/v1/bookings", json=body(room_id))).json()
    clock.advance(seconds=301)

    response = await client.post(f"/api/v1/bookings/{booking['id']}/confirm")

    assert (response.status_code, response.json()["error"]) == (410, "hold_expired")
    assert (await client.get(f"/api/v1/bookings/{booking['id']}")).json()["status"] == "expired"
    assert (await client.post("/api/v1/bookings", json=body(room_id))).status_code == 201


async def test_a_lapsed_hold_is_replaced_by_the_next_request(
    client: httpx.AsyncClient, room_id: Room, clock: FakeClock
) -> None:
    first = (await client.post("/api/v1/bookings", json=body(room_id))).json()
    clock.advance(seconds=301)  # the sweeper has NOT run

    second = await client.post("/api/v1/bookings", json=body(room_id, guest_email="b@example.com"))

    assert second.status_code == 201
    assert (await client.get(f"/api/v1/bookings/{first['id']}")).json()["status"] == "expired"


async def test_a_live_hold_is_not_stolen(
    client: httpx.AsyncClient, room_id: Room, clock: FakeClock
) -> None:
    await client.post("/api/v1/bookings", json=body(room_id))
    clock.advance(seconds=299)
    clash = await client.post("/api/v1/bookings", json=body(room_id, guest_email="b@example.com"))
    assert clash.status_code == 409


# ------------------------------------------------------------------------------- lifecycle
async def test_cancel_frees_the_slot_and_is_idempotent(
    client: httpx.AsyncClient, room_id: Room
) -> None:
    booking = (await client.post("/api/v1/bookings", json=body(room_id))).json()

    first = await client.post(f"/api/v1/bookings/{booking['id']}/cancel")
    again = await client.post(f"/api/v1/bookings/{booking['id']}/cancel")

    assert (first.json()["status"], again.json()["status"]) == ("cancelled", "cancelled")
    assert (await client.post("/api/v1/bookings", json=body(room_id))).status_code == 201


async def test_confirm_is_idempotent_and_a_cancelled_booking_cannot_be_confirmed(
    client: httpx.AsyncClient, room_id: Room
) -> None:
    booking = (await client.post("/api/v1/bookings", json=body(room_id))).json()
    await client.post(f"/api/v1/bookings/{booking['id']}/confirm")
    assert (await client.post(f"/api/v1/bookings/{booking['id']}/confirm")).status_code == 200

    other = (await client.post("/api/v1/bookings", json=body(room_id, 120))).json()
    await client.post(f"/api/v1/bookings/{other['id']}/cancel")
    response = await client.post(f"/api/v1/bookings/{other['id']}/confirm")
    assert (response.status_code, response.json()["error"]) == (409, "invalid_state_transition")


async def test_concurrent_confirm_and_cancel_end_consistent(
    client: httpx.AsyncClient, room_id: Room
) -> None:
    booking = (await client.post("/api/v1/bookings", json=body(room_id))).json()

    results = await asyncio.gather(
        client.post(f"/api/v1/bookings/{booking['id']}/confirm"),
        client.post(f"/api/v1/bookings/{booking['id']}/cancel"),
    )

    assert all(r.status_code in (200, 409) for r in results)
    final = (await client.get(f"/api/v1/bookings/{booking['id']}")).json()["status"]
    assert final in ("confirmed", "cancelled")


# ------------------------------------------------------------------------------ idempotency
async def test_idempotency_key_replays_the_same_booking(
    client: httpx.AsyncClient, room_id: Room
) -> None:
    headers = {"Idempotency-Key": "abcdef123456"}
    first = await client.post("/api/v1/bookings", json=body(room_id), headers=headers)
    replay = await client.post("/api/v1/bookings", json=body(room_id), headers=headers)

    assert (first.status_code, replay.status_code) == (201, 200)
    assert first.json()["id"] == replay.json()["id"]

    other = await client.post("/api/v1/bookings", json=body(room_id, 120), headers=headers)
    assert (other.status_code, other.json()["error"]) == (422, "idempotency_key_reuse")


async def test_concurrent_identical_requests_create_one_booking(
    client: httpx.AsyncClient, room_id: Room
) -> None:
    headers = {"Idempotency-Key": "race-key-0001"}
    responses = await asyncio.gather(
        *(client.post("/api/v1/bookings", json=body(room_id), headers=headers) for _ in range(10))
    )

    assert all(r.status_code in (200, 201) for r in responses), [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    assert sum(r.status_code == 201 for r in responses) == 1
