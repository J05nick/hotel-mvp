"""Calendar read model: which rooms/bookings the board shows for a window."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from uuid import UUID

import httpx
import pytest

from app.application.booking_service import CreateHoldCommand
from app.application.calendar_service import CalendarService
from app.domain.enums import BookingStatus, RoomStatus
from app.domain.exceptions import InvalidCalendarWindowError
from app.domain.value_objects import BLOCK, TimeSlot
from app.infrastructure.db.repositories import SqlAlchemyUnitOfWork
from app.main import create_app
from app.presentation.api.deps import get_calendar_service
from tests.integration.conftest import Harness
from tests.support import T0

pytestmark = pytest.mark.integration

DAY_START = T0.replace(hour=0)
DAY_END = DAY_START + timedelta(days=1)


@pytest.fixture
def calendar(harness: Harness) -> CalendarService:
    return CalendarService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(harness.session_factory), clock=harness.clock
    )


async def _book(h: Harness, room_id: UUID, start_block: int, blocks: int = 2) -> UUID:
    slot = TimeSlot.from_blocks(T0 + BLOCK * start_block, blocks)
    result = await h.service.create_hold(
        CreateHoldCommand(room_id, slot, "Ada Lovelace", f"g{start_block}@example.com")
    )
    return result.booking.id


def _room(calendar_result, room_id: UUID):  # type: ignore[no-untyped-def]
    return next(r for r in calendar_result.rooms if r.room.id == room_id)


async def test_lists_rooms_with_their_visible_bookings(
    harness: Harness, calendar: CalendarService, room_id: UUID
) -> None:
    pending = await _book(harness, room_id, 0)
    confirmed = await _book(harness, room_id, 2)
    await harness.service.confirm(confirmed)

    result = await calendar.get(DAY_START, DAY_END)

    entry = _room(result, room_id)
    assert [b.id for b in entry.bookings] == [pending, confirmed]  # chronological
    assert [b.status for b in entry.bookings] == [BookingStatus.PENDING, BookingStatus.CONFIRMED]
    assert result.generated_at == harness.clock()


async def test_cancelled_expired_and_lapsed_holds_are_not_shown(
    harness: Harness, calendar: CalendarService, room_id: UUID
) -> None:
    cancelled = await _book(harness, room_id, 0)
    await harness.service.cancel(cancelled)
    await _book(harness, room_id, 2)  # will be a lapsed hold
    harness.clock.advance(seconds=301)  # Celery has NOT swept it yet
    visible = await _book(harness, room_id, 4)  # fresh hold, created after the clock moved

    result = await calendar.get(DAY_START, DAY_END)

    assert [b.id for b in _room(result, room_id).bookings] == [visible]


async def test_window_is_half_open_and_only_overlapping_bookings_count(
    harness: Harness, calendar: CalendarService, room_id: UUID
) -> None:
    await _book(harness, room_id, 0, blocks=2)  # 10:00-11:00
    inside = await _book(harness, room_id, 2, blocks=2)  # 11:00-12:00

    # Window 11:00-12:00: the first ends exactly at 11:00 (excluded), the second matches.
    result = await calendar.get(T0 + BLOCK * 2, T0 + BLOCK * 4)
    assert [b.id for b in _room(result, room_id).bookings] == [inside]

    # A booking straddling the window edge is still returned.
    result = await calendar.get(T0 + BLOCK * 3, T0 + BLOCK * 5)
    assert [b.id for b in _room(result, room_id).bookings] == [inside]


async def test_rooms_without_bookings_are_listed_with_their_status(
    harness: Harness, calendar: CalendarService, make_room: Callable[..., UUID]
) -> None:
    cleaning = make_room(status=RoomStatus.CLEANING)

    result = await calendar.get(DAY_START, DAY_END)

    entry = _room(result, cleaning)
    assert entry.room.status is RoomStatus.CLEANING
    assert entry.bookings == []


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (DAY_START, DAY_START),
        (DAY_END, DAY_START),
        (DAY_START, DAY_START + timedelta(days=15)),
        (DAY_START.replace(tzinfo=None), DAY_END.replace(tzinfo=None)),
    ],
    ids=["empty", "inverted", "too-wide", "naive"],
)
async def test_invalid_windows_are_rejected(calendar: CalendarService, start, end) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidCalendarWindowError):
        await calendar.get(start, end)


# ------------------------------------------------------------------------- HTTP
@pytest.fixture
async def client(calendar: CalendarService) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_calendar_service] = lambda: calendar
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def test_http_contract_and_privacy(
    harness: Harness, client: httpx.AsyncClient, room_id: UUID
) -> None:
    booking_id = await _book(harness, room_id, 0)

    response = await client.get(
        "/api/v1/calendar", params={"from": DAY_START.isoformat(), "to": DAY_END.isoformat()}
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"generated_at", "start", "end", "rooms"}
    room = next(r for r in body["rooms"] if r["id"] == str(room_id))
    assert set(room) == {
        "id", "number", "name", "capacity", "price_per_block_cents", "status", "bookings"
    }  # fmt: skip
    assert room["bookings"] == [
        {
            "id": str(booking_id),
            "status": "pending",
            "start": "2031-03-01T10:00:00Z",
            "end": "2031-03-01T11:00:00Z",
            "hold_expires_at": "2031-02-28T10:05:00Z",
        }
    ]
    # The calendar is unauthenticated: no guest data may leak through it.
    assert "Ada" not in response.text
    assert "example.com" not in response.text


async def test_http_validation(client: httpx.AsyncClient) -> None:
    too_wide = await client.get(
        "/api/v1/calendar",
        params={"from": DAY_START.isoformat(), "to": (DAY_START + timedelta(days=30)).isoformat()},
    )
    assert (too_wide.status_code, too_wide.json()["error"]) == (422, "invalid_window")

    assert (await client.get("/api/v1/calendar")).status_code == 422  # missing params
    naive = await client.get(
        "/api/v1/calendar", params={"from": "2031-03-01T00:00:00", "to": "2031-03-02T00:00:00"}
    )
    assert naive.status_code == 422
