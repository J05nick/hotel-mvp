"""FastAPI application factory (composition root)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.infrastructure.cache.redis_client import get_redis
from app.infrastructure.db.session import get_async_engine
from app.infrastructure.events.subscriber import EventSubscriber
from app.presentation.api.bookings import router as bookings_router
from app.presentation.api.calendar import router as calendar_router
from app.presentation.api.errors import register_error_handlers
from app.presentation.api.rooms import router as rooms_router
from app.presentation.ws.manager import ConnectionManager
from app.presentation.ws.router import router as ws_router

logger = logging.getLogger(__name__)


class HealthStatus(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool | None = None
    redis: bool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info("app.startup", extra={"environment": settings.environment.value})

    manager = ConnectionManager(max_queue=settings.ws_max_queue)
    app.state.connection_manager = manager
    # Pub/sub needs its own connection with NO read timeout: the shared client's
    # socket_timeout would abort the idle subscription.
    pubsub_redis = Redis.from_url(
        str(settings.redis_url), decode_responses=True, health_check_interval=30
    )
    subscriber = EventSubscriber(
        pubsub_redis,
        settings.events_channel,
        on_event=manager.broadcast,
        on_resync=lambda: manager.broadcast_system("system.resync"),
    )
    subscriber_task = asyncio.create_task(subscriber.run(), name="events-subscriber")
    try:
        yield
    finally:
        # 12-Factor IX (Disposability): release resources on SIGTERM for fast, clean shutdown.
        subscriber_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscriber_task
        await manager.close_all()
        await pubsub_redis.aclose()
        await get_redis().aclose()
        await get_async_engine().dispose()
        logger.info("app.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    # Configure before uvicorn emits its first records so the whole stream is JSON.
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Hotel MVP API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_error_handlers(app)
    app.include_router(bookings_router)
    app.include_router(calendar_router)
    app.include_router(rooms_router)
    app.include_router(ws_router)

    @app.get("/health/live", response_model=HealthStatus, tags=["health"])
    async def liveness() -> HealthStatus:
        return HealthStatus(status="ok")

    @app.get("/health/ready", response_model=HealthStatus, tags=["health"])
    async def readiness(response: Response) -> HealthStatus:
        db_ok = redis_ok = False
        try:
            async with get_async_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            logger.exception("health.database_unreachable")
        try:
            redis_ok = bool(await get_redis().ping())
        except Exception:
            logger.exception("health.redis_unreachable")

        healthy = db_ok and redis_ok
        if not healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthStatus(status="ok" if healthy else "degraded", database=db_ok, redis=redis_ok)

    return app


app = create_app()
