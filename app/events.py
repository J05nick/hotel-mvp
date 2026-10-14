"""Real-time events: an in-memory hub that fans messages out to connected WebSockets.

Demo simplification: the hub lives in the process, so it works with ONE API process. With
several workers each would only reach its own browsers (the full edition uses Redis pub/sub).

Message envelope (version 1), identical to the full edition so the same frontend works:
    {"v": 1, "type": "booking.confirmed", "occurred_at": "...", "room_id": "...", "data": {...}}
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.models import Booking, BookingStatus

logger = logging.getLogger(__name__)

EVENT_FOR_STATUS = {
    BookingStatus.PENDING: "booking.hold_created",
    BookingStatus.CONFIRMED: "booking.confirmed",
    BookingStatus.CANCELLED: "booking.cancelled",
    BookingStatus.COMPLETED: "booking.completed",
    BookingStatus.EXPIRED: "booking.expired",
}


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def envelope(type_: str, room_id: UUID | None, data: dict[str, Any], at: datetime) -> str:
    return json.dumps(
        {
            "v": 1,
            "type": type_,
            "occurred_at": iso(at),
            "room_id": str(room_id) if room_id else None,
            "data": data,
        }
    )


@dataclass(eq=False)
class Client:
    room_ids: frozenset[UUID] | None  # None = every room
    queue: asyncio.Queue[str]
    dropped: bool = field(default=False)


class EventHub:
    def __init__(self, max_queue: int = 256) -> None:
        self._max_queue = max_queue
        self._clients: set[Client] = set()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def subscribe(self, room_ids: frozenset[UUID] | None = None) -> Client:
        client = Client(room_ids, asyncio.Queue(maxsize=self._max_queue))
        self._clients.add(client)
        return client

    def unsubscribe(self, client: Client) -> None:
        self._clients.discard(client)

    def _send(self, message: str, room_id: UUID | None) -> None:
        for client in tuple(self._clients):
            if client.room_ids is not None and room_id not in client.room_ids:
                continue
            try:
                client.queue.put_nowait(message)
            except asyncio.QueueFull:
                # A client that cannot keep up is cut loose: it reconnects and refetches.
                client.dropped = True
                self._clients.discard(client)
                logger.warning("ws client too slow, dropped")

    def booking(self, booking: Booking, at: datetime) -> None:
        start, end = booking.during.lower, booking.during.upper
        assert start is not None and end is not None
        data = {
            "booking_id": str(booking.id),
            "status": booking.status.value,
            "start": iso(start),
            "end": iso(end),
            "hold_expires_at": iso(booking.hold_expires_at) if booking.hold_expires_at else None,
        }
        self._send(
            envelope(EVENT_FOR_STATUS[booking.status], booking.room_id, data, at), booking.room_id
        )

    def room_status(self, room_id: UUID, status: str, previous: str | None, at: datetime) -> None:
        data = {"status": status, "previous_status": previous}
        self._send(envelope("room.status_changed", room_id, data, at), room_id)
