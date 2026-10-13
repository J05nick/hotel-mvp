"""Read model: rooms and their bookings inside a time window (the calendar board)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from app.application.ports import UnitOfWork
from app.domain.entities import Booking, Room
from app.domain.exceptions import InvalidCalendarWindowError

MAX_WINDOW: Final[timedelta] = timedelta(days=14)


@dataclass(frozen=True, slots=True)
class RoomCalendar:
    room: Room
    bookings: list[Booking]


@dataclass(frozen=True, slots=True)
class Calendar:
    generated_at: datetime
    start: datetime
    end: datetime
    rooms: list[RoomCalendar]


class CalendarService:
    def __init__(
        self, uow_factory: Callable[[], UnitOfWork], clock: Callable[[], datetime]
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def get(self, start: datetime, end: datetime) -> Calendar:
        """Snapshot of every room with the bookings overlapping `[start, end)`."""
        if start.tzinfo is None or end.tzinfo is None:
            raise InvalidCalendarWindowError("from and to must be timezone-aware")
        if end <= start:
            raise InvalidCalendarWindowError("to must be after from")
        if end - start > MAX_WINDOW:
            raise InvalidCalendarWindowError(f"the window cannot exceed {MAX_WINDOW.days} days")

        now = self._clock()
        async with self._uow_factory() as uow:
            rooms = await uow.rooms.list_all()
            bookings = await uow.bookings.list_visible_in_window(start, end, now)

        by_room: dict[UUID, list[Booking]] = defaultdict(list)
        for booking in bookings:
            by_room[booking.room_id].append(booking)
        return Calendar(
            generated_at=now,
            start=start,
            end=end,
            rooms=[RoomCalendar(room, by_room[room.id]) for room in rooms],
        )
