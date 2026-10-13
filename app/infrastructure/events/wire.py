"""Wire format of real-time events (JSON over Redis pub/sub and WebSocket).

Envelope (version 1):
    {"v": 1, "type": "booking.confirmed", "occurred_at": "...", "room_id": "...",
     "data": {...}}

`data` depends on `type`:
    booking.*            -> {booking_id, status, start, end, hold_expires_at}
    room.status_changed  -> {status, previous_status}
    system.connected     -> {}     (sent by the WebSocket server, not Redis)
    system.resync        -> {}     (events may have been lost: refetch the snapshot)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.domain.events import BookingEvent, DomainEvent, EventType


class BookingData(BaseModel):
    model_config = ConfigDict(frozen=True)

    booking_id: UUID
    status: str
    start: datetime
    end: datetime
    hold_expires_at: datetime | None


class RoomStatusData(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    previous_status: str | None


class WireEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    v: Literal[1] = 1
    type: str
    occurred_at: datetime
    room_id: UUID | None
    data: BookingData | RoomStatusData | dict[str, str]


def to_wire(event: DomainEvent) -> WireEvent:
    if isinstance(event, BookingEvent):
        return WireEvent(
            type=event.type.value,
            occurred_at=event.occurred_at,
            room_id=event.room_id,
            data=BookingData(
                booking_id=event.booking_id,
                status=event.status.value,
                start=event.start,
                end=event.end,
                hold_expires_at=event.hold_expires_at,
            ),
        )
    return WireEvent(
        type=EventType.ROOM_STATUS_CHANGED.value,
        occurred_at=event.occurred_at,
        room_id=event.room_id,
        data=RoomStatusData(
            status=event.status.value,
            previous_status=event.previous_status.value if event.previous_status else None,
        ),
    )


def system_message(kind: Literal["system.connected", "system.resync"], now: datetime) -> str:
    return WireEvent(type=kind, occurred_at=now, room_id=None, data={}).model_dump_json()
