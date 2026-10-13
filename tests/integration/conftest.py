"""Integration fixtures: a throw-away PostgreSQL database migrated to head.

Set TEST_DATABASE_URL to a server where the role may CREATE DATABASE, e.g.
    TEST_DATABASE_URL=postgresql+psycopg://hotel:hotel@localhost:5432/postgres
A uniquely named database is created per session and dropped afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.application.booking_service import BookingService
from app.application.housekeeping_service import HousekeepingService
from app.application.ports import SlotHoldStore
from app.domain.enums import RoomStatus
from app.infrastructure.cache.slot_holds import RedisSlotHoldStore
from app.infrastructure.db.models import RoomModel
from app.infrastructure.db.repositories import SqlAlchemyUnitOfWork
from tests.support import FakeClock, RecordingPublisher

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    cfg.cmd_opts = type("Opts", (), {"x": [f"db_url={db_url}"]})()
    return cfg


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL not set")

    name = f"hotel_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(_alembic_config(url), "head")
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    eng = create_engine(database_url, pool_size=10)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def alembic_cfg(database_url: str) -> Config:
    return _alembic_config(database_url)


# --------------------------------------------------------------------------- async + Redis
@pytest.fixture(scope="session")
def redis_url() -> str:
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL not set")
    return url


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
async def session_factory(
    database_url: str, engine: Engine
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # NullPool: connections are never reused across event loops (one loop per test).
    async_engine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(async_engine, expire_on_commit=False, autoflush=False)
    await async_engine.dispose()


@pytest.fixture
def make_room(engine: Engine) -> Callable[..., UUID]:
    def _make(status: RoomStatus = RoomStatus.AVAILABLE, price: int = 1_000) -> UUID:
        with Session(engine) as session:
            room = RoomModel(
                number=uuid.uuid4().hex[:8],
                name="Test room",
                capacity=2,
                price_per_block_cents=price,
                status=status,
            )
            session.add(room)
            session.commit()
            return room.id

    return _make


@pytest.fixture
def room_id(make_room: Callable[..., UUID]) -> UUID:
    return make_room()


@dataclass
class Harness:
    service: BookingService
    clock: FakeClock
    events: RecordingPublisher
    redis: Redis
    session_factory: async_sessionmaker[AsyncSession]
    holds: SlotHoldStore


@pytest.fixture
def make_harness(
    redis: Redis, session_factory: async_sessionmaker[AsyncSession]
) -> Callable[..., Harness]:
    def _make(holds: SlotHoldStore | None = None, ttl: int = 300) -> Harness:
        clock = FakeClock()
        events = RecordingPublisher()
        store = holds or RedisSlotHoldStore(redis)
        service = BookingService(
            uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
            holds=store,
            events=events,
            hold_ttl_seconds=ttl,
            clock=clock,
        )
        return Harness(service, clock, events, redis, session_factory, store)

    return _make


@pytest.fixture
def harness(make_harness: Callable[..., Harness]) -> Harness:
    return make_harness()


@dataclass
class Housekeeping:
    service: HousekeepingService
    events: RecordingPublisher


@pytest.fixture
def housekeeping(harness: Harness) -> Housekeeping:
    """Housekeeping sharing the booking harness' clock, database and Redis."""
    events = RecordingPublisher()
    service = HousekeepingService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(harness.session_factory),
        events=events,
        clock=harness.clock,
    )
    return Housekeeping(service, events)
