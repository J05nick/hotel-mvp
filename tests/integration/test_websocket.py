"""WebSocket endpoint: Redis pub/sub -> browsers, with Origin check and back-pressure."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest
import redis as sync_redis
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.domain.enums import BookingStatus, RoomStatus
from app.domain.events import BookingEvent, EventType, RoomStatusEvent
from app.infrastructure.cache.redis_client import get_redis
from app.infrastructure.events.wire import to_wire
from app.main import create_app
from app.presentation.ws.manager import CLOSE_TRY_AGAIN_LATER, ConnectionManager
from tests.support import T0

pytestmark = pytest.mark.integration


class _Env:
    def __init__(self, client: TestClient, redis_url: str, channel: str) -> None:
        self.client = client
        self._pub = sync_redis.Redis.from_url(redis_url)
        self.channel = channel

    def wait_for_subscriber(self) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            counts = cast("list[Any]", self._pub.pubsub_numsub(self.channel))
            if counts and counts[0][1] >= 1:
                return
            time.sleep(0.02)
        raise AssertionError("API never subscribed to the events channel")

    def publish(self, event: BookingEvent | RoomStatusEvent) -> None:
        self._pub.publish(self.channel, to_wire(event).model_dump_json())


@contextmanager
def _connect(env: _Env, path: str = "/ws/events", **kwargs: Any) -> Iterator[WebSocketTestSession]:
    """Open a socket and, on exit, wait until the server finished tearing it down.

    Starlette's TestClient cancels the app's scope right after sending the disconnect,
    without waiting for the handler's cleanup; leaving the block early would race it.
    """
    manager = cast("ConnectionManager", env.client.app.state.connection_manager)  # type: ignore[attr-defined]
    with env.client.websocket_connect(path, **kwargs) as ws:
        sessions_with_me = manager.active_sessions
        try:
            yield ws
        finally:
            ws.close()
            deadline = time.monotonic() + 5
            while manager.active_sessions >= sessions_with_me and time.monotonic() < deadline:
                time.sleep(0.005)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, redis_url: str) -> Iterator[_Env]:
    channel = f"test:events:{uuid.uuid4().hex}"
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("EVENTS_CHANNEL", channel)
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000")
    get_settings.cache_clear()
    get_redis.cache_clear()
    with TestClient(create_app()) as client:
        yield _Env(client, redis_url, channel)
    get_settings.cache_clear()
    get_redis.cache_clear()


def _booking_event(room_id: uuid.UUID) -> BookingEvent:
    return BookingEvent(
        type=EventType.BOOKING_CONFIRMED,
        booking_id=uuid.uuid4(),
        room_id=room_id,
        status=BookingStatus.CONFIRMED,
        start=T0,
        end=T0.replace(hour=11),
        occurred_at=T0,
    )


def test_client_receives_booking_and_room_events(env: _Env) -> None:
    room = uuid.uuid4()
    with _connect(env) as ws:
        assert ws.receive_json()["type"] == "system.connected"
        env.wait_for_subscriber()

        env.publish(_booking_event(room))
        env.publish(
            RoomStatusEvent(
                room_id=room,
                status=RoomStatus.CLEANING,
                previous_status=RoomStatus.OCCUPIED,
                occurred_at=T0,
            )
        )

        booking_msg, room_msg = ws.receive_json(), ws.receive_json()
        assert booking_msg["v"] == 1
        assert booking_msg["type"] == "booking.confirmed"
        assert booking_msg["room_id"] == str(room)
        assert booking_msg["data"]["status"] == "confirmed"
        assert room_msg["type"] == "room.status_changed"
        assert room_msg["data"] == {"status": "cleaning", "previous_status": "occupied"}


def test_room_filter_only_delivers_subscribed_rooms(env: _Env) -> None:
    wanted, other = uuid.uuid4(), uuid.uuid4()
    with _connect(env, f"/ws/events?room_id={wanted}") as ws:
        assert ws.receive_json()["type"] == "system.connected"
        env.wait_for_subscriber()

        env.publish(_booking_event(other))  # must be filtered out
        env.publish(_booking_event(wanted))

        assert ws.receive_json()["room_id"] == str(wanted)


def test_event_fans_out_to_every_client(env: _Env) -> None:
    room = uuid.uuid4()
    with (
        _connect(env) as a,
        _connect(env) as b,
    ):
        a.receive_json()
        b.receive_json()
        env.wait_for_subscriber()
        env.publish(_booking_event(room))
        assert a.receive_json()["room_id"] == str(room)
        assert b.receive_json()["room_id"] == str(room)


def test_foreign_origin_is_rejected(env: _Env) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        env.client.websocket_connect("/ws/events", headers={"origin": "https://evil.example"}),
    ):
        pass
    assert exc_info.value.code == 1008


def test_allowed_origin_is_accepted(env: _Env) -> None:
    with _connect(env, headers={"origin": "http://localhost:3000"}) as ws:
        assert ws.receive_json()["type"] == "system.connected"


def test_idle_subscription_survives_longer_than_the_shared_client_timeout(env: _Env) -> None:
    """The shared Redis client has socket_timeout=5; the subscriber must not inherit it."""
    with _connect(env) as ws:
        ws.receive_json()
        env.wait_for_subscriber()
        time.sleep(6.5)
        room = uuid.uuid4()
        env.publish(_booking_event(room))
        assert ws.receive_json()["room_id"] == str(room)


# ------------------------------------------------------------------ manager unit
class _StalledSocket:
    """A browser that stopped reading: send_text never completes."""

    def __init__(self) -> None:
        self.closed_with: int | None = None
        self._hang = asyncio.Event()

    async def send_text(self, _: str) -> None:
        await self._hang.wait()

    async def receive_text(self) -> str:
        await self._hang.wait()
        return ""

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code
        self._hang.set()


async def test_slow_client_is_dropped_without_blocking_broadcast() -> None:
    manager = ConnectionManager(max_queue=3)
    socket = _StalledSocket()
    serving = asyncio.create_task(manager.serve(cast("WebSocket", socket), None))
    await asyncio.sleep(0.05)
    assert manager.connection_count == 1

    for i in range(10):  # more than the queue can hold; must never block or raise
        manager.broadcast(f'{{"n": {i}}}', room_id=None)
    await asyncio.wait_for(serving, timeout=2)

    assert socket.closed_with == CLOSE_TRY_AGAIN_LATER
    assert manager.connection_count == 0
