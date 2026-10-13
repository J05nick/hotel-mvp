"""Engine and session factories.

- Async engine/sessions: FastAPI request handlers and WebSocket handlers.
- Sync engine/sessions: Celery tasks and Alembic (no event loop there).
Both use psycopg 3 and the same per-connection safety timeouts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings


def _engine_kwargs(settings: Settings) -> dict[str, Any]:
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout_seconds,
        "pool_pre_ping": True,
        **_connect_kwargs(settings),
    }


def build_task_engine(settings: Settings) -> AsyncEngine:
    """Engine for ONE short-lived event loop (Celery tasks run `asyncio.run` per call).

    NullPool: connections must never outlive the loop that created them.
    """
    return create_async_engine(
        str(settings.database_url), poolclass=NullPool, **_connect_kwargs(settings)
    )


def _connect_kwargs(settings: Settings) -> dict[str, Any]:
    return {
        "connect_args": {
            # CONCURRENCY: bound how long any statement may wait for a row lock
            # (e.g. SELECT ... FOR UPDATE on a contended room). Failing fast is
            # preferable to exhausting the pool under a thundering herd.
            "options": (
                f"-c lock_timeout={settings.db_lock_timeout_ms} "
                f"-c statement_timeout={settings.db_statement_timeout_ms} "
                "-c timezone=UTC"
            ),
            "application_name": settings.app_name,
        },
    }


@lru_cache(maxsize=1)
def get_async_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(str(settings.database_url), **_engine_kwargs(settings))


@lru_cache(maxsize=1)
def get_async_sessionmaker() -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: entities stay readable after commit (we map them to
    # domain objects / DTOs after the transaction closes).
    return async_sessionmaker(get_async_engine(), expire_on_commit=False, autoflush=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session (and at most one transaction) per request."""
    async with get_async_sessionmaker()() as session:
        yield session


@lru_cache(maxsize=1)
def get_sync_engine() -> Engine:
    settings = get_settings()
    return create_engine(str(settings.database_url), **_engine_kwargs(settings))


@lru_cache(maxsize=1)
def get_sync_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_sync_engine(), expire_on_commit=False, autoflush=False)
