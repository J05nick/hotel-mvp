"""Celery tasks end to end: real clock, real PostgreSQL, events observed on Redis pub/sub."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis as sync_redis
from sqlalchemy import Engine, select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain.enums import BookingStatus, RoomStatus
from app.infrastructure.db.models import BookingModel, RoomModel
from app.infrastructure.tasks.housekeeping import complete_finished_bookings, expire_stale_holds

pytestmark = pytest.mark.integration

HALF_HOUR = timedelta(minutes=30)


class _Listener:
    def __init__(self, url: str, channel: str) -> None:
        # Keep a reference: when the client is garbage collected it closes the pub/sub socket.
        self._client: sync_redis.Redis = sync_redis.Redis.from_url(url, decode_responses=True)
        self._pubsub = self._client.pubsub()  # type: ignore[no-untyped-call]
        self._pubsub.subscribe(channel)
        self._pubsub.get_message(timeout=2)  # consume the subscribe confirmation

    def drain(self, expected: int, timeout: float = 3.0) -> list[dict[str, Any]]:
        received: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        while len(received) < expected and time.monotonic() < deadline:
            message = self._pubsub.get_message(timeout=0.2)
            if message and message["type"] == "message":
                received.append(json.loads(message["data"]))
        return received


@pytest.fixture
def listener(
    monkeypatch: pytest.MonkeyPatch, database_url: str, redis_url: str, engine: Engine
) -> Iterator[_Listener]:
    channel = f"test:tasks:{uuid.uuid4().hex}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("EVENTS_CHANNEL", channel)
    get_settings.cache_clear()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM bookings"))
    yield _Listener(redis_url, channel)
    get_settings.cache_clear()


def _half_hour_floor(moment: datetime) -> datetime:
    return moment.replace(minute=moment.minute // 30 * 30, second=0, microsecond=0)


def _insert_room_and_booking(
    engine: Engine,
    *,
    status: BookingStatus,
    start: datetime,
    hold_expires_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    with Session(engine) as session:
        room = RoomModel(
            number=uuid.uuid4().hex[:8], name="Task room", capacity=2, price_per_block_cents=1_000
        )
        session.add(room)
        session.flush()
        booking = BookingModel(
            room_id=room.id,
            guest_name="Ada",
            guest_email="ada@example.com",
            during=Range(start, start + HALF_HOUR, bounds="[)"),
            status=status,
            total_price_cents=1_000,
            hold_expires_at=hold_expires_at,
        )
        session.add(booking)
        session.commit()
        return room.id, booking.id


def test_complete_task_marks_room_for_cleaning_and_emits_websocket_events(
    engine: Engine, listener: _Listener
) -> None:
    start = _half_hour_floor(datetime.now(UTC)) - timedelta(hours=2)
    room_id, booking_id = _insert_room_and_booking(
        engine, status=BookingStatus.CONFIRMED, start=start
    )

    result = complete_finished_bookings.apply().get()

    assert result == {"bookings_completed": 1, "rooms_set_cleaning": 1, "rooms_skipped": 0}
    with Session(engine) as session:
        assert session.get_one(BookingModel, booking_id).status is BookingStatus.COMPLETED
        assert session.get_one(RoomModel, room_id).status is RoomStatus.CLEANING

    events = listener.drain(expected=2)
    assert [e["type"] for e in events] == ["booking.completed", "room.status_changed"]
    assert events[1]["room_id"] == str(room_id)
    assert events[1]["data"] == {"status": "cleaning", "previous_status": "available"}

    # Next Beat tick: nothing left to do, nothing re-emitted.
    assert complete_finished_bookings.apply().get()["bookings_completed"] == 0
    assert listener.drain(expected=1, timeout=0.5) == []


def test_complete_task_leaves_in_progress_bookings_alone(
    engine: Engine, listener: _Listener
) -> None:
    start = _half_hour_floor(datetime.now(UTC) + HALF_HOUR)  # starts in the future
    room_id, booking_id = _insert_room_and_booking(
        engine, status=BookingStatus.CONFIRMED, start=start
    )

    assert complete_finished_bookings.apply().get()["bookings_completed"] == 0

    with Session(engine) as session:
        assert session.get_one(BookingModel, booking_id).status is BookingStatus.CONFIRMED
        assert session.get_one(RoomModel, room_id).status is RoomStatus.AVAILABLE
    assert listener.drain(expected=1, timeout=0.5) == []


def test_expire_task_releases_lapsed_holds(engine: Engine, listener: _Listener) -> None:
    start = _half_hour_floor(datetime.now(UTC)) + timedelta(hours=3)
    _, booking_id = _insert_room_and_booking(
        engine,
        status=BookingStatus.PENDING,
        start=start,
        hold_expires_at=datetime.now(UTC) - timedelta(seconds=5),
    )

    assert expire_stale_holds.apply().get() == 1

    with Session(engine) as session:
        row = session.execute(
            select(BookingModel.status).where(BookingModel.id == booking_id)
        ).scalar_one()
        assert row is BookingStatus.EXPIRED
    events = listener.drain(expected=1)
    assert [e["type"] for e in events] == ["booking.expired"]
    assert events[0]["data"]["booking_id"] == str(booking_id)
