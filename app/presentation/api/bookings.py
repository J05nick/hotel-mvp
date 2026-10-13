"""Booking endpoints (presenters): translate HTTP <-> application commands."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Response, status

from app.application.booking_service import CreateHoldCommand
from app.domain.value_objects import TimeSlot
from app.presentation.api.deps import BookingServiceDep
from app.presentation.api.schemas import BookingResponse, CreateHoldRequest, ErrorResponse

router = APIRouter(prefix="/api/v1/bookings", tags=["bookings"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.post(
    "",
    response_model=BookingResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**_ERRORS, 200: {"model": BookingResponse, "description": "Idempotent replay"}},
    summary="Hold a time range for 5 minutes (the reservation cart)",
)
async def create_hold(
    body: CreateHoldRequest,
    response: Response,
    service: BookingServiceDep,
    idempotency_key: Annotated[str | None, Header(min_length=8, max_length=64)] = None,
) -> BookingResponse:
    result = await service.create_hold(
        CreateHoldCommand(
            room_id=body.room_id,
            slot=TimeSlot(body.start, body.end),  # InvalidTimeSlotError -> 422
            guest_name=body.guest_name,
            guest_email=str(body.guest_email),
            idempotency_key=idempotency_key,
        )
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return BookingResponse.from_entity(result.booking)


@router.get("/{booking_id}", response_model=BookingResponse, responses=_ERRORS)
async def get_booking(booking_id: UUID, service: BookingServiceDep) -> BookingResponse:
    return BookingResponse.from_entity(await service.get(booking_id))


@router.post(
    "/{booking_id}/confirm",
    response_model=BookingResponse,
    responses={**_ERRORS, 410: {"model": ErrorResponse}},
    summary="Confirm a held booking (PENDING -> CONFIRMED)",
)
async def confirm_booking(booking_id: UUID, service: BookingServiceDep) -> BookingResponse:
    return BookingResponse.from_entity(await service.confirm(booking_id))


@router.post(
    "/{booking_id}/cancel",
    response_model=BookingResponse,
    responses=_ERRORS,
    summary="Cancel a booking and free its time range",
)
async def cancel_booking(booking_id: UUID, service: BookingServiceDep) -> BookingResponse:
    return BookingResponse.from_entity(await service.cancel(booking_id))
