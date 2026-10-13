"""HTTP request/response models (pydantic v2)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field

from app.domain.entities import Booking
from app.domain.enums import BookingStatus


class CreateHoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_id: UUID
    # Half-open range [start, end). Both must be tz-aware and on :00 / :30 boundaries.
    start: AwareDatetime
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
    def from_entity(cls, booking: Booking) -> BookingResponse:
        return cls(
            id=booking.id,
            room_id=booking.room_id,
            status=booking.status,
            start=booking.slot.start,
            end=booking.slot.end,
            blocks=booking.slot.blocks,
            total_price_cents=booking.total_price_cents,
            hold_expires_at=booking.hold_expires_at,
            guest_name=booking.guest_name,
            guest_email=booking.guest_email,
        )


class ErrorResponse(BaseModel):
    error: str
    detail: str
