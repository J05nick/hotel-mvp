"""Room endpoints (presenters)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from app.presentation.api.deps import RoomServiceDep
from app.presentation.api.schemas import ErrorResponse, RoomResponse

router = APIRouter(prefix="/api/v1/rooms", tags=["rooms"])


@router.post(
    "/{room_id}/cleaned",
    response_model=RoomResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Mark a room as clean (CLEANING -> AVAILABLE)",
)
async def finish_cleaning(room_id: UUID, service: RoomServiceDep) -> RoomResponse:
    # Staff-only action: put an authentication dependency here before exposing it publicly.
    return RoomResponse.from_entity(await service.finish_cleaning(room_id))
