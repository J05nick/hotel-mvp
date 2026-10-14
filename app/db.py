"""Engine, sessions and schema creation (no migrations: this is the demo edition)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models import Base, Room, RoomStatus

SessionFactory = async_sessionmaker[AsyncSession]


def make_engine(database_url: str) -> tuple[AsyncEngine, SessionFactory]:
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        # A request waiting on a row lock fails fast instead of hanging the demo.
        connect_args={"options": "-c lock_timeout=5000 -c timezone=UTC"},
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create the extension and tables if they do not exist yet."""
    async with engine.begin() as conn:
        # Serialise concurrent starters (e.g. two uvicorn workers) on the DDL below.
        await conn.execute(text("SELECT pg_advisory_xact_lock(7041202601)"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        await conn.run_sync(Base.metadata.create_all)


DEMO_ROOMS = [
    ("101", "Garden suite", 2, 1500, RoomStatus.AVAILABLE),
    ("102", "Sea view", 4, 2500, RoomStatus.CLEANING),
    ("103", "Attic studio", 2, 1000, RoomStatus.MAINTENANCE),
    ("104", "Courtyard", 3, 1800, RoomStatus.AVAILABLE),
]


async def seed_demo_rooms(session_factory: SessionFactory) -> None:
    """Insert a few rooms when the table is empty, so the demo has something to show."""
    async with session_factory() as session:
        if (await session.scalar(select(func.count()).select_from(Room))) == 0:
            session.add_all(
                Room(number=n, name=name, capacity=cap, price_per_block_cents=price, status=st)
                for n, name, cap, price, st in DEMO_ROOMS
            )
            await session.commit()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request."""
    factory: SessionFactory = request.app.state.session_factory
    async with factory() as session:
        yield session
