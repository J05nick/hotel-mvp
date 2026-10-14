"""FastAPI application: wiring, startup (schema, demo data, sweeper) and error handling."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg import errors as pg_errors
from sqlalchemy.exc import OperationalError

from app import services
from app.config import Settings, get_settings
from app.db import SessionFactory, init_db, make_engine, seed_demo_rooms
from app.errors import ApiError
from app.events import EventHub
from app.routers import api, health, ws_router

logger = logging.getLogger("hotel")


async def housekeeping_loop(
    factory: SessionFactory, hub: EventHub, clock: Callable[[], datetime], interval: int
) -> None:
    """Replaces the scheduled worker of the full edition: a task inside the API process."""
    while True:
        try:
            now = clock()
            await services.expire_stale_holds(factory, hub, now)
            await services.complete_finished_bookings(factory, hub, now)
        except Exception:
            logger.exception("housekeeping sweep failed")  # keep sweeping
        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine, factory = make_engine(settings.database_url)
        app.state.session_factory = factory
        await init_db(engine)
        if settings.seed_demo_data:
            await seed_demo_rooms(factory)

        sweeper: asyncio.Task[None] | None = None
        if settings.housekeeping_interval_seconds > 0:
            sweeper = asyncio.create_task(
                housekeeping_loop(
                    factory, app.state.hub, app.state.clock, settings.housekeeping_interval_seconds
                )
            )
        try:
            yield
        finally:
            if sweeper:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper
            await engine.dispose()

    app = FastAPI(title="Hotel MVP (lite)", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.hub = EventHub()
    app.state.clock = services.utcnow  # tests replace it with a controllable clock

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api)
    app.include_router(health)
    app.include_router(ws_router)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error": exc.code, "detail": exc.detail}
        )

    @app.exception_handler(OperationalError)
    async def _db_busy(_: Request, exc: OperationalError) -> JSONResponse:
        if isinstance(exc.orig, pg_errors.LockNotAvailable):
            return JSONResponse(
                status_code=503,
                content={"error": "busy", "detail": "database is busy, retry shortly"},
                headers={"Retry-After": "1"},
            )
        raise exc

    return app


app = create_app()
