"""Domain error -> HTTP translation (the only place that knows both worlds)."""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.exceptions import (
    DomainError,
    HoldExpiredError,
    IdempotencyKeyReuseError,
    InvalidStateTransitionError,
    InvalidTimeSlotError,
    NotFoundError,
    ResourceBusyError,
    RoomNotBookableError,
    SlotUnavailableError,
)

# Most specific first: the first isinstance match wins.
_STATUS_BY_ERROR: Final[tuple[tuple[type[DomainError], int, str], ...]] = (
    (NotFoundError, status.HTTP_404_NOT_FOUND, "not_found"),
    (InvalidTimeSlotError, status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_time_slot"),
    (SlotUnavailableError, status.HTTP_409_CONFLICT, "slot_unavailable"),
    (IdempotencyKeyReuseError, status.HTTP_422_UNPROCESSABLE_CONTENT, "idempotency_key_reuse"),
    (InvalidStateTransitionError, status.HTTP_409_CONFLICT, "invalid_state_transition"),
    (RoomNotBookableError, status.HTTP_409_CONFLICT, "room_not_bookable"),
    (HoldExpiredError, status.HTTP_410_GONE, "hold_expired"),
    (ResourceBusyError, status.HTTP_503_SERVICE_UNAVAILABLE, "busy"),
)


async def _domain_error_handler(_: Request, exc: Exception) -> JSONResponse:
    for error_type, http_status, code in _STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            headers = {"Retry-After": "1"} if isinstance(exc, ResourceBusyError) else None
            return JSONResponse(
                status_code=http_status,
                content={"error": code, "detail": str(exc)},
                headers=headers,
            )
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": "domain_error", "detail": str(exc)},
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error_handler)
