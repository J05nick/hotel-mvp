"""Real-time events: the in-memory hub and the /ws/events endpoint."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.events import EventHub
from app.main import create_app
from app.models import Room, RoomStatus
from tests.conftest import FakeClock, body

# ------------------------------------------------------------------------------ the hub
ROOM_A, ROOM_B = uuid.uuid4(), uuid.uuid4()


def test_hub_delivers_only_to_interested_clients() -> None:
    hub = EventHub()
    everyone = hub.subscribe()
    only_a = hub.subscribe(frozenset({ROOM_A}))

    hub._send("for-b", ROOM_B)
    hub._send("for-a", ROOM_A)

    assert [everyone.queue.get_nowait(), everyone.queue.get_nowait()] == ["for-b", "for-a"]
    assert only_a.queue.get_nowait() == "for-a"
    assert only_a.queue.empty()


def test_a_client_that_cannot_keep_up_is_dropped_without_blocking_the_rest() -> None:
    hub = EventHub(max_queue=2)
    slow = hub.subscribe()
    healthy = hub.subscribe()

    for i in range(5):
        hub._send(f"m{i}", ROOM_A)
        healthy.queue.get_nowait()  # the healthy client keeps reading

    assert slow.dropped
    assert hub.client_count == 1  # only the healthy one remains


def test_unsubscribe_stops_delivery() -> None:
    hub = EventHub()
    client = hub.subscribe()
    hub.unsubscribe(client)
    hub._send("x", ROOM_A)
    assert client.queue.empty()


# ------------------------------------------------------------------------- the endpoint
@pytest.fixture
def web(database_url: str) -> Iterator[tuple[TestClient, uuid.UUID, uuid.UUID]]:
    """A real app on its own event loop plus two fresh rooms (cleaning, available)."""
    sync_url = database_url
    engine = create_engine(sync_url)
    with Session(engine) as session:
        cleaning = Room(
            number=uuid.uuid4().hex[:8], name="A", capacity=2, price_per_block_cents=1000,
            status=RoomStatus.CLEANING,
        )  # fmt: skip
        other = Room(number=uuid.uuid4().hex[:8], name="B", capacity=2, price_per_block_cents=1000)
        session.add_all([cleaning, other])
        session.commit()
        ids = (cleaning.id, other.id)
    engine.dispose()

    app = create_app(Settings(database_url=database_url, housekeeping_interval_seconds=0))
    app.state.clock = FakeClock()
    with TestClient(app) as client:
        yield client, ids[0], ids[1]


@contextmanager
def connect(
    client: TestClient, path: str = "/ws/events", **kw: Any
) -> Iterator[WebSocketTestSession]:
    """Open a socket; on exit wait until the server finished with it.

    Starlette's TestClient cancels the app right after sending the disconnect, so leaving
    the block early would race the handler's own cleanup.
    """
    hub: EventHub = client.app.state.hub  # type: ignore[attr-defined]
    with client.websocket_connect(path, **kw) as ws:
        before = hub.client_count
        try:
            yield ws
        finally:
            ws.close()
            deadline = time.monotonic() + 5
            while hub.client_count >= before and time.monotonic() < deadline:
                time.sleep(0.005)


def test_a_browser_sees_the_whole_stay_live(web: tuple[TestClient, uuid.UUID, uuid.UUID]) -> None:
    client, cleaning, other = web
    with connect(client) as ws:
        assert ws.receive_json()["type"] == "system.connected"

        booking = client.post("/api/v1/bookings", json=body(other)).json()
        held = ws.receive_json()
        assert (held["v"], held["type"], held["room_id"]) == (1, "booking.hold_created", str(other))
        assert held["data"]["booking_id"] == booking["id"]
        assert held["data"]["status"] == "pending"
        assert held["data"]["hold_expires_at"] is not None

        client.post(f"/api/v1/bookings/{booking['id']}/confirm")
        assert ws.receive_json()["type"] == "booking.confirmed"

        client.post(f"/api/v1/rooms/{cleaning}/cleaned")
        room_event = ws.receive_json()
        assert room_event["type"] == "room.status_changed"
        assert room_event["data"] == {"status": "available", "previous_status": "cleaning"}

        client.post(f"/api/v1/bookings/{booking['id']}/cancel")
        assert ws.receive_json()["type"] == "booking.cancelled"


def test_the_room_filter_only_delivers_that_room(
    web: tuple[TestClient, uuid.UUID, uuid.UUID],
) -> None:
    client, cleaning, other = web
    with connect(client, f"/ws/events?room_id={other}") as ws:
        assert ws.receive_json()["type"] == "system.connected"

        client.post(f"/api/v1/rooms/{cleaning}/cleaned")  # other room: filtered out
        client.post("/api/v1/bookings", json=body(other))

        assert ws.receive_json()["room_id"] == str(other)


def test_a_foreign_origin_is_rejected_and_an_allowed_one_accepted(
    web: tuple[TestClient, uuid.UUID, uuid.UUID],
) -> None:
    client = web[0]
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/ws/events", headers={"origin": "https://evil.example"}),
    ):
        pass
    assert refused.value.code == 1008

    with connect(client, headers={"origin": "http://localhost:3000"}) as ws:
        assert ws.receive_json()["type"] == "system.connected"
