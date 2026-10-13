"""HTTP layer end-to-end (ASGI in-process) on top of the real service."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.main import create_app
from app.presentation.api.deps import get_booking_service
from tests.integration.conftest import Harness
from tests.support import T0

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(harness: Harness) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_booking_service] = lambda: harness.service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def _body(room_id: UUID, start_min: int = 0, minutes: int = 60, **extra: Any) -> dict[str, Any]:
    start = T0 + timedelta(minutes=start_min)
    return {
        "room_id": str(room_id),
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
        "guest_name": "Ada Lovelace",
        "guest_email": "ada@example.com",
        **extra,
    }


async def test_hold_confirm_flow(client: httpx.AsyncClient, room_id: UUID) -> None:
    created = await client.post("/api/v1/bookings", json=_body(room_id))
    assert created.status_code == 201
    booking = created.json()
    assert booking["status"] == "pending"
    assert booking["blocks"] == 2
    assert booking["total_price_cents"] == 2_000
    assert booking["hold_expires_at"] is not None

    confirmed = await client.post(f"/api/v1/bookings/{booking['id']}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"

    fetched = await client.get(f"/api/v1/bookings/{booking['id']}")
    assert fetched.json()["status"] == "confirmed"


async def test_overlap_returns_409(client: httpx.AsyncClient, room_id: UUID) -> None:
    assert (await client.post("/api/v1/bookings", json=_body(room_id))).status_code == 201

    clash = await client.post("/api/v1/bookings", json=_body(room_id, start_min=30))

    assert clash.status_code == 409
    assert clash.json()["error"] == "slot_unavailable"


async def test_idempotency_key_replay_returns_200(client: httpx.AsyncClient, room_id: UUID) -> None:
    headers = {"Idempotency-Key": "abcdef123456"}
    first = await client.post("/api/v1/bookings", json=_body(room_id), headers=headers)
    replay = await client.post("/api/v1/bookings", json=_body(room_id), headers=headers)

    assert (first.status_code, replay.status_code) == (201, 200)
    assert first.json()["id"] == replay.json()["id"]

    mismatch = await client.post(
        "/api/v1/bookings", json=_body(room_id, start_min=120), headers=headers
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"] == "idempotency_key_reuse"


@pytest.mark.parametrize(
    ("start_min", "minutes"),
    [(15, 30), (0, 45), (0, 0), (0, -30)],
    ids=["misaligned-start", "partial-block", "empty", "inverted"],
)
async def test_invalid_time_slot_returns_422(
    client: httpx.AsyncClient, room_id: UUID, start_min: int, minutes: int
) -> None:
    response = await client.post("/api/v1/bookings", json=_body(room_id, start_min, minutes))
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_time_slot"


async def test_naive_datetime_and_bad_email_fail_validation(
    client: httpx.AsyncClient, room_id: UUID
) -> None:
    naive = _body(room_id) | {"start": "2031-03-01T10:00:00", "end": "2031-03-01T11:00:00"}
    assert (await client.post("/api/v1/bookings", json=naive)).status_code == 422
    bad_email = _body(room_id, guest_email="not-an-email")
    assert (await client.post("/api/v1/bookings", json=bad_email)).status_code == 422


async def test_unknown_resources_return_404(client: httpx.AsyncClient, room_id: UUID) -> None:
    ghost = uuid.uuid4()
    assert (await client.post("/api/v1/bookings", json=_body(ghost))).status_code == 404
    assert (await client.get(f"/api/v1/bookings/{ghost}")).status_code == 404
    assert (await client.post(f"/api/v1/bookings/{ghost}/confirm")).status_code == 404
    assert (await client.post(f"/api/v1/bookings/{ghost}/cancel")).status_code == 404


async def test_confirm_after_expiry_returns_410(
    client: httpx.AsyncClient, harness: Harness, room_id: UUID
) -> None:
    booking = (await client.post("/api/v1/bookings", json=_body(room_id))).json()
    harness.clock.advance(seconds=301)

    response = await client.post(f"/api/v1/bookings/{booking['id']}/confirm")

    assert response.status_code == 410
    assert response.json()["error"] == "hold_expired"


async def test_cancel_then_confirm_conflicts(
    client: httpx.AsyncClient, room_id: UUID, make_room: Callable[..., UUID]
) -> None:
    booking = (await client.post("/api/v1/bookings", json=_body(room_id))).json()
    assert (await client.post(f"/api/v1/bookings/{booking['id']}/cancel")).status_code == 200

    response = await client.post(f"/api/v1/bookings/{booking['id']}/confirm")

    assert response.status_code == 409
    assert response.json()["error"] == "invalid_state_transition"
