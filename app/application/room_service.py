"""Room status operations performed by hotel staff."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.application.ports import EventPublisher, UnitOfWork
from app.domain.entities import Room
from app.domain.enums import RoomStatus
from app.domain.events import RoomStatusEvent
from app.domain.exceptions import InvalidStateTransitionError, RoomNotFoundError

logger = logging.getLogger(__name__)


class RoomService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        events: EventPublisher,
        clock: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._events = events
        self._clock = clock

    async def finish_cleaning(self, room_id: UUID) -> Room:
        """CLEANING -> AVAILABLE once housekeeping staff has finished the room.

        Idempotent: an already AVAILABLE room is returned untouched and emits nothing, so
        retries and double clicks are harmless. Rooms in any other state (OCCUPIED,
        MAINTENANCE) raise InvalidStateTransitionError.
        """
        now = self._clock()
        async with self._uow_factory() as uow:
            # CONCURRENCY: exclusive row lock. The Celery sweep that flips a room to
            # CLEANING and concurrent clicks on this endpoint queue up behind each other;
            # the status read below is therefore never stale.
            room = await uow.rooms.get_for_update(room_id)
            if room is None:
                raise RoomNotFoundError(str(room_id))
            if room.status is RoomStatus.AVAILABLE:
                return room

            if room.status is not RoomStatus.CLEANING:
                # MAINTENANCE -> AVAILABLE is a valid transition in general, but it must
                # not be reachable through "mark clean": that room was never being cleaned.
                raise InvalidStateTransitionError(
                    f"room {room.number}: only a room in cleaning can be marked clean "
                    f"(is {room.status})"
                )

            previous = room.status
            room.transition_to(RoomStatus.AVAILABLE)
            await uow.rooms.save(room)
            await uow.commit()

        await self._events.publish(
            [
                RoomStatusEvent(
                    room_id=room.id,
                    status=room.status,
                    previous_status=previous,
                    occurred_at=now,
                )
            ]
        )
        logger.info("room.cleaning_finished", extra={"room_id": str(room.id)})
        return room
