"""Fixtures: a throw-away PostgreSQL database and an app wired to it.

Set TEST_DATABASE_URL to a server where the role may CREATE DATABASE, e.g.
    TEST_DATABASE_URL=postgresql+psycopg://hotel:hotel@localhost:5432/postgres
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.main import create_app
from app.models import Room, RoomStatus

T0 = datetime(2031, 3, 1, 10, 0, tzinfo=UTC)  # a Saturday, 10:00 UTC


class FakeClock:
    """Starts a day before T0, so bookings at T0 are in the future until a test moves time."""

    def __init__(self) -> None:
        self.now = T0 - timedelta(days=1)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL not set")
    name = f"hotel_lite_{uuid.uuid4().hex[:8]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def app(database_url: str, clock: FakeClock) -> AsyncIterator[FastAPI]:
    application = create_app(
        Settings(
            database_url=database_url,
            housekeeping_interval_seconds=0,  # tests run the sweeps by hand
            seed_demo_data=False,
            cors_origins=["http://localhost:3000"],
        )
    )
    application.state.clock = clock
    async with application.router.lifespan_context(application):
        async with application.state.session_factory() as session:
            await session.execute(text("TRUNCATE bookings, rooms CASCADE"))
            await session.commit()
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def make_room(app: FastAPI) -> Callable[..., Awaitable[uuid.UUID]]:
    async def _make(status: RoomStatus = RoomStatus.AVAILABLE, price: int = 1_000) -> uuid.UUID:
        async with app.state.session_factory() as session:
            room = Room(
                number=uuid.uuid4().hex[:8],
                name="Test room",
                capacity=2,
                price_per_block_cents=price,
                status=status,
            )
            session.add(room)
            await session.commit()
            return room.id

    return _make


@pytest.fixture
async def room_id(make_room: Callable[..., Awaitable[uuid.UUID]]) -> uuid.UUID:
    return await make_room()


def body(
    room: uuid.UUID, start_min: int = 0, minutes: int = 60, **extra: object
) -> dict[str, object]:
    """A POST /bookings payload starting `start_min` minutes after T0."""
    start = T0 + timedelta(minutes=start_min)
    return {
        "room_id": str(room),
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
        "guest_name": "Ada Lovelace",
        "guest_email": "ada@example.com",
        **extra,
    }
