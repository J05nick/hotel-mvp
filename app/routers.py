"""HTTP and WebSocket endpoints. Thin: parse, call a service, shape the response."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import AwareDatetime
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.config import Settings
from app.db import get_session
from app.events import EventHub, envelope
from app.schemas import (
    BookingResponse,
    CalendarResponse,
    CreateHoldRequest,
    RoomResponse,
)

Session = Annotated[AsyncSession, Depends(get_session)]


def hub_of(request: Request) -> EventHub:
    hub: EventHub = request.app.state.hub
    return hub


def now_of(request: Request) -> datetime:
    clock: Callable[[], datetime] = request.app.state.clock
    return clock()


Hub = Annotated[EventHub, Depends(hub_of)]
Now = Annotated[datetime, Depends(now_of)]

api = APIRouter(prefix="/api/v1")


@api.post("/bookings", response_model=BookingResponse, status_code=201, tags=["bookings"])
async def create_hold(
    body: CreateHoldRequest,
    request: Request,
    response: Response,
    session: Session,
    hub: Hub,
    now: Now,
    idempotency_key: Annotated[str | None, Header(min_length=8, max_length=64)] = None,
) -> BookingResponse:
    """Hold a time range for 5 minutes (the reservation cart). 200 on an idempotent replay."""
    settings: Settings = request.app.state.settings
    booking, created = await services.create_hold(
        session,
        hub,
        room_id=body.room_id,
        start=body.start,
        end=body.end,
        guest_name=body.guest_name,
        guest_email=str(body.guest_email),
        idempotency_key=idempotency_key,
        ttl_seconds=settings.booking_hold_ttl_seconds,
        now=now,
    )
    if not created:
        response.status_code = 200
    return BookingResponse.of(booking)


@api.get("/bookings/{booking_id}", response_model=BookingResponse, tags=["bookings"])
async def get_booking(booking_id: UUID, session: Session) -> BookingResponse:
    return BookingResponse.of(await services.get_booking(session, booking_id))


@api.post("/bookings/{booking_id}/confirm", response_model=BookingResponse, tags=["bookings"])
async def confirm_booking(
    booking_id: UUID, session: Session, hub: Hub, now: Now
) -> BookingResponse:
    return BookingResponse.of(await services.confirm_booking(session, hub, booking_id, now))


@api.post("/bookings/{booking_id}/cancel", response_model=BookingResponse, tags=["bookings"])
async def cancel_booking(booking_id: UUID, session: Session, hub: Hub, now: Now) -> BookingResponse:
    return BookingResponse.of(await services.cancel_booking(session, hub, booking_id, now))


@api.post("/rooms/{room_id}/cleaned", response_model=RoomResponse, tags=["rooms"])
async def finish_cleaning(room_id: UUID, session: Session, hub: Hub, now: Now) -> RoomResponse:
    """Staff action: cleaning -> available. (No authentication in the demo.)"""
    return RoomResponse.of(await services.finish_cleaning(session, hub, room_id, now))


@api.get("/calendar", response_model=CalendarResponse, tags=["calendar"])
async def get_calendar(
    session: Session,
    now: Now,
    start: Annotated[AwareDatetime, Query(alias="from")],
    end: Annotated[AwareDatetime, Query(alias="to")],
) -> CalendarResponse:
    return await services.calendar(session, start, end, now)


# --------------------------------------------------------------------------------- health
health = APIRouter(prefix="/health", tags=["health"])


@health.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@health.get("/ready")
async def ready(session: Session, response: Response) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return {"status": "degraded"}
    return {"status": "ok"}


# ------------------------------------------------------------------------------- websocket
ws_router = APIRouter(tags=["realtime"])


@ws_router.websocket("/ws/events")
async def events_socket(
    websocket: WebSocket, room_id: Annotated[list[UUID] | None, Query()] = None
) -> None:
    """Server -> client stream of booking and room events (optionally filtered by room)."""
    settings: Settings = websocket.app.state.settings
    hub: EventHub = websocket.app.state.hub
    clock: Callable[[], datetime] = websocket.app.state.clock

    # Browsers do not apply CORS to WebSockets: check the Origin ourselves.
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in settings.cors_origins:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    client = hub.subscribe(frozenset(room_id) if room_id else None)
    client.queue.put_nowait(envelope("system.connected", None, {}, clock()))

    # Wait on two things at once: an outgoing event, or the peer going away.
    incoming = asyncio.create_task(websocket.receive_text())
    outgoing = asyncio.create_task(client.queue.get())
    try:
        while True:
            done, _ = await asyncio.wait({incoming, outgoing}, return_when=asyncio.FIRST_COMPLETED)
            if incoming in done:
                incoming.result()  # raises WebSocketDisconnect when the peer closed
                incoming = asyncio.create_task(websocket.receive_text())  # ignore stray text
            if outgoing in done:
                await asyncio.wait_for(websocket.send_text(outgoing.result()), timeout=10)
                if client.dropped:
                    await websocket.close(code=1013)  # too slow: reconnect and refetch
                    break
                outgoing = asyncio.create_task(client.queue.get())
    except (WebSocketDisconnect, TimeoutError):
        pass
    finally:
        hub.unsubscribe(client)
        incoming.cancel()
        outgoing.cancel()
