"""WebSocket endpoint: server -> client stream of booking / room state changes.

    ws://host/ws/events                     every room
    ws://host/ws/events?room_id=<uuid>&room_id=<uuid>   only those rooms

The channel is read-only for clients. See `app.infrastructure.events.wire` for the
message catalogue. Authentication is intentionally out of scope for the MVP; put an
auth dependency here (token in the query string / first message) before production.
"""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, status

from app.core.config import get_settings
from app.presentation.ws.manager import ConnectionManager

router = APIRouter(tags=["realtime"])


@router.websocket("/ws/events")
async def events_socket(
    websocket: WebSocket,
    room_id: Annotated[list[UUID] | None, Query()] = None,
) -> None:
    # Browsers don't apply CORS to WebSockets, so verify the Origin ourselves to stop
    # arbitrary web pages from subscribing on a visitor's behalf. Non-browser clients
    # (no Origin header) are allowed.
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in get_settings().cors_origins:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    manager = cast("ConnectionManager", websocket.app.state.connection_manager)
    await websocket.accept()
    await manager.serve(websocket, frozenset(room_id) if room_id else None)
