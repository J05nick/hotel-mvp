"""Calendar read endpoint: the initial snapshot for the board (live changes: /ws/events)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import AwareDatetime

from app.presentation.api.deps import CalendarServiceDep
from app.presentation.api.schemas import CalendarResponse, ErrorResponse

router = APIRouter(prefix="/api/v1/calendar", tags=["calendar"])

_ERRORS: dict[int | str, dict[str, Any]] = {422: {"model": ErrorResponse}}


@router.get(
    "",
    response_model=CalendarResponse,
    responses=_ERRORS,
    summary="Rooms with the bookings overlapping a time window",
)
async def get_calendar(
    service: CalendarServiceDep,
    start: Annotated[AwareDatetime, Query(alias="from", description="Window start (tz-aware)")],
    end: Annotated[
        AwareDatetime, Query(alias="to", description="Window end (tz-aware, exclusive)")
    ],
) -> CalendarResponse:
    return CalendarResponse.from_calendar(await service.get(start, end))
