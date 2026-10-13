"""Scheduled housekeeping (driven by Celery Beat, FASE 3).

Two idempotent sweeps, safe to run concurrently and to run twice:

* `complete_finished_bookings` — every CONFIRMED / CHECKED_IN booking whose range
  ended becomes COMPLETED and its room is flagged CLEANING.
* `expire_stale_holds` — PENDING bookings whose 5-minute hold lapsed become EXPIRED
  (the API already expires the ones that block a new request; this frees the rest).

Concurrency
-----------
* One transaction PER ROOM: a failure or lock timeout in one room never rolls back or
  delays the others, and the room lock is held for milliseconds.
* Lock order is rooms -> bookings (same as the API), so there are no deadlocks.
* `FOR UPDATE SKIP LOCKED` everywhere: when two workers (or two overlapping ticks) meet,
  the loser skips what the winner holds. Because every transition checks the current
  status, running a sweep twice changes nothing the second time.
* Events are published after each commit, never before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.application.events import booking_event
from app.application.ports import EventPublisher, UnitOfWork
from app.domain.enums import BookingStatus, RoomStatus
from app.domain.events import DomainEvent, RoomStatusEvent
from app.domain.exceptions import ResourceBusyError

logger = logging.getLogger(__name__)

# Rooms in these states become CLEANING after a stay; MAINTENANCE and CLEANING are left alone.
_DIRTYABLE: frozenset[RoomStatus] = frozenset({RoomStatus.AVAILABLE, RoomStatus.OCCUPIED})


@dataclass(frozen=True, slots=True)
class CompletionReport:
    bookings_completed: int = 0
    rooms_set_cleaning: int = 0
    rooms_skipped: int = 0  # locked by another worker / busy: retried next tick


@dataclass(frozen=True, slots=True)
class _RoomOutcome:
    events: list[DomainEvent]
    completed: int
    cleaning: bool


class HousekeepingService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        events: EventPublisher,
        clock: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._events = events
        self._clock = clock

    # ------------------------------------------------------ finished bookings
    async def complete_finished_bookings(self, max_rooms: int = 200) -> CompletionReport:
        now = self._clock()
        async with self._uow_factory() as uow:
            room_ids = await uow.bookings.rooms_with_finished_bookings(now, max_rooms)

        completed = cleaning = skipped = 0
        for room_id in room_ids:
            try:
                outcome = await self._complete_room(room_id, now)
            except ResourceBusyError:
                logger.warning("housekeeping.room_busy", extra={"room_id": str(room_id)})
                skipped += 1
                continue
            if outcome is None:
                skipped += 1
                continue
            completed += outcome.completed
            cleaning += int(outcome.cleaning)
            await self._events.publish(outcome.events)

        report = CompletionReport(completed, cleaning, skipped)
        logger.info(
            "housekeeping.completed",
            extra={
                "rooms": len(room_ids),
                "bookings_completed": completed,
                "rooms_set_cleaning": cleaning,
                "rooms_skipped": skipped,
            },
        )
        return report

    async def _complete_room(self, room_id: UUID, now: datetime) -> _RoomOutcome | None:
        events: list[DomainEvent] = []
        async with self._uow_factory() as uow:
            # Room first (lock order), and SKIP LOCKED: if another worker is already
            # cleaning this room's books, there is nothing for us to do.
            room = await uow.rooms.get_for_update(room_id, skip_locked=True)
            if room is None:
                return None

            finished = await uow.bookings.list_finished_for_update(room_id, now)
            if not finished:
                return _RoomOutcome([], 0, cleaning=False)

            for booking in finished:
                booking.transition_to(BookingStatus.COMPLETED)
                await uow.bookings.save(booking)
                events.append(booking_event(booking, now))

            # Back-to-back stays: while the next guest is already in the room it is not
            # dirty-and-empty, so it only flips to CLEANING when the last stay ends.
            goes_dirty = room.status in _DIRTYABLE and not await uow.bookings.has_active_booking(
                room_id, now
            )
            if goes_dirty:
                previous = room.status
                room.transition_to(RoomStatus.CLEANING)
                await uow.rooms.save(room)
                events.append(
                    RoomStatusEvent(
                        room_id=room.id,
                        status=room.status,
                        previous_status=previous,
                        occurred_at=now,
                    )
                )
            await uow.commit()
        return _RoomOutcome(events, len(finished), goes_dirty)

    # ------------------------------------------------------------ stale holds
    async def expire_stale_holds(self, batch_size: int = 500) -> int:
        now = self._clock()
        events: list[DomainEvent] = []
        async with self._uow_factory() as uow:
            stale = await uow.bookings.list_expired_holds_for_update(now, batch_size)
            for booking in stale:
                booking.transition_to(BookingStatus.EXPIRED)
                await uow.bookings.save(booking)
                events.append(booking_event(booking, now))
            await uow.commit()

        # Redis cart keys carry the same 5-minute TTL, so they are already gone.
        await self._events.publish(events)
        logger.info("housekeeping.holds_expired", extra={"count": len(stale)})
        return len(stale)
