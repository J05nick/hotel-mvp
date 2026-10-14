"""Request and response models."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field

from app.models import BLOCK_MINUTES, Booking, BookingStatus, Room, RoomStatus


class CreateHoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_id: UUID
    start: AwareDatetime  # [start, end) on :00 / :30 boundaries
    end: AwareDatetime
    guest_name: str = Field(min_length=1, max_length=120)
    guest_email: EmailStr = Field(max_length=254)


class BookingResponse(BaseModel):
    id: UUID
    room_id: UUID
    status: BookingStatus
    start: datetime
    end: datetime
    blocks: int
    total_price_cents: int
    hold_expires_at: datetime | None
    guest_name: str
    guest_email: str

    @classmethod
    def of(cls, booking: Booking) -> BookingResponse:
        start, end = booking.during.lower, booking.during.upper
        assert start is not None and end is not None
        return cls(
            id=booking.id,
            room_id=booking.room_id,
            status=booking.status,
            start=start,
            end=end,
            blocks=int((end - start) / timedelta(minutes=BLOCK_MINUTES)),
            total_price_cents=booking.total_price_cents,
            hold_expires_at=booking.hold_expires_at,
            guest_name=booking.guest_name,
            guest_email=booking.guest_email,
        )


class RoomResponse(BaseModel):
    id: UUID
    number: str
    name: str
    capacity: int
    price_per_block_cents: int
    status: RoomStatus

    @classmethod
    def of(cls, room: Room) -> RoomResponse:
        return cls(
            id=room.id,
            number=room.number,
            name=room.name,
            capacity=room.capacity,
            price_per_block_cents=room.price_per_block_cents,
            status=room.status,
        )


class CalendarBooking(BaseModel):
    """Occupancy only: the calendar is public, so it never carries guest data."""

    id: UUID
    status: BookingStatus
    start: datetime
    end: datetime
    hold_expires_at: datetime | None


class CalendarRoom(RoomResponse):
    bookings: list[CalendarBooking]


class CalendarResponse(BaseModel):
    generated_at: datetime
    start: datetime
    end: datetime
    rooms: list[CalendarRoom]
