"""Booking use cases: hold ("cart") -> confirm / cancel.

Concurrency design (defence in depth, outermost first)
-------------------------------------------------------
1. Redis hold (`SET NX EX 300` per 30-min block): cheap, user-facing gate that
   rejects the thundering herd before it reaches PostgreSQL and gives the guest
   a 5-minute "cart" window.
2. PostgreSQL `FOR SHARE` on the room row: bookers don't block each other, but
   the room cannot be taken out of service mid-booking.
3. PostgreSQL EXCLUDE USING gist: the authoritative, race-free guarantee. If
   Redis is down, flushed or bypassed, an overlapping INSERT still fails.
4. `FOR UPDATE` on the booking row for confirm/cancel (pessimistic) plus the
   `version_id` column (optimistic) so API and Celery cannot lose each other's updates.

Events are published only AFTER the transaction commits.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from app.application.ports import EventPublisher, SlotHoldStore, UnitOfWork
from app.domain.entities import Booking
from app.domain.enums import BookingStatus, RoomStatus
from app.domain.events import BookingEvent, DomainEvent, EventType
from app.domain.exceptions import (
    BookingNotFoundError,
    DuplicateIdempotencyKeyError,
    HoldExpiredError,
    IdempotencyKeyReuseError,
    RoomNotBookableError,
    RoomNotFoundError,
    SlotUnavailableError,
)
from app.domain.value_objects import TimeSlot

logger = logging.getLogger(__name__)

_EVENT_FOR_STATUS: Final[dict[BookingStatus, EventType]] = {
    BookingStatus.PENDING: EventType.BOOKING_HOLD_CREATED,
    BookingStatus.CONFIRMED: EventType.BOOKING_CONFIRMED,
    BookingStatus.CANCELLED: EventType.BOOKING_CANCELLED,
    BookingStatus.EXPIRED: EventType.BOOKING_EXPIRED,
}


@dataclass(frozen=True, slots=True)
class CreateHoldCommand:
    room_id: UUID
    slot: TimeSlot
    guest_name: str
    guest_email: str
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class HoldResult:
    booking: Booking
    created: bool  # False => an Idempotency-Key replay returned the stored booking


def _booking_event(booking: Booking, now: datetime) -> BookingEvent:
    return BookingEvent(
        type=_EVENT_FOR_STATUS[booking.status],
        booking_id=booking.id,
        room_id=booking.room_id,
        status=booking.status,
        start=booking.slot.start,
        end=booking.slot.end,
        occurred_at=now,
        hold_expires_at=booking.hold_expires_at,
    )


class BookingService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        holds: SlotHoldStore,
        events: EventPublisher,
        hold_ttl_seconds: int,
        clock: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._holds = holds
        self._events = events
        self._hold_ttl = hold_ttl_seconds
        self._clock = clock

    # ------------------------------------------------------------------ hold
    async def create_hold(self, cmd: CreateHoldCommand) -> HoldResult:
        """Reserve `cmd.slot` for `hold_ttl_seconds` as a PENDING booking."""
        if cmd.idempotency_key is not None:
            replay = await self._replay(cmd)
            if replay is not None:
                return HoldResult(replay, created=False)

        # Validate the room BEFORE touching Redis: otherwise anyone could fill Redis
        # with hold keys for random room ids (memory exhaustion under `noeviction`).
        async with self._uow_factory() as uow:
            room = await uow.rooms.get(cmd.room_id)
        if room is None:
            raise RoomNotFoundError(str(cmd.room_id))
        if room.status is RoomStatus.MAINTENANCE:
            raise RoomNotBookableError(f"room {room.number} is under maintenance")

        # The booking id doubles as the Redis lock owner => release is a pure
        # function of (room, slot, booking id); no extra token needs storing.
        booking_id = uuid4()

        # --- LAYER 1: distributed lock (the 5-minute cart) -----------------------
        if not await self._holds.acquire(cmd.room_id, cmd.slot, booking_id, self._hold_ttl):
            if cmd.idempotency_key is not None:
                # A retry of our own request may have won the race a moment ago.
                replay = await self._replay(cmd)
                if replay is not None:
                    return HoldResult(replay, created=False)
            raise SlotUnavailableError("another guest is holding part of this range")

        events: list[DomainEvent] = []
        try:
            booking = await self._persist_hold(cmd, booking_id, events)
        except DuplicateIdempotencyKeyError:
            # Two identical requests raced past the replay check; the other one won.
            await self._release(cmd.room_id, cmd.slot, booking_id)
            assert cmd.idempotency_key is not None
            replay = await self._replay(cmd)
            if replay is None:  # pragma: no cover - key row vanished between statements
                raise
            return HoldResult(replay, created=False)
        except BaseException:
            # Any failure (conflict, timeout, cancellation): give the blocks back at once
            # instead of making the guest wait for the TTL.
            await asyncio.shield(self._release(cmd.room_id, cmd.slot, booking_id))
            raise

        await self._events.publish(events)
        return HoldResult(booking, created=True)

    async def _persist_hold(
        self, cmd: CreateHoldCommand, booking_id: UUID, events: list[DomainEvent]
    ) -> Booking:
        async with self._uow_factory() as uow:
            # --- LAYER 2: pessimistic shared lock on the room row ------------------
            room = await uow.rooms.get_for_share(cmd.room_id)
            if room is None:
                raise RoomNotFoundError(str(cmd.room_id))
            if room.status is RoomStatus.MAINTENANCE:
                raise RoomNotBookableError(f"room {room.number} is under maintenance")

            now = self._clock()
            for stale in await uow.bookings.expire_stale_holds(room.id, cmd.slot, now):
                events.append(_booking_event(stale, now))

            booking = Booking(
                id=booking_id,
                room_id=room.id,
                guest_name=cmd.guest_name,
                guest_email=cmd.guest_email,
                slot=cmd.slot,
                total_price_cents=room.price_per_block_cents * cmd.slot.blocks,
                status=BookingStatus.PENDING,
                hold_expires_at=now + timedelta(seconds=self._hold_ttl),
                idempotency_key=cmd.idempotency_key,
            )
            # --- LAYER 3: EXCLUDE USING gist decides who owns the range ------------
            await uow.bookings.add(booking)
            await uow.commit()
            events.append(_booking_event(booking, now))
            return booking

    async def _replay(self, cmd: CreateHoldCommand) -> Booking | None:
        assert cmd.idempotency_key is not None
        async with self._uow_factory() as uow:
            existing = await uow.bookings.get_by_idempotency_key(cmd.idempotency_key)
        if existing is None:
            return None
        same_request = (
            existing.room_id == cmd.room_id
            and existing.slot == cmd.slot
            and existing.guest_email == cmd.guest_email
        )
        if not same_request:
            raise IdempotencyKeyReuseError("Idempotency-Key was used with different parameters")
        return existing

    # --------------------------------------------------------------- confirm
    async def confirm(self, booking_id: UUID) -> Booking:
        """PENDING -> CONFIRMED. Idempotent for already confirmed bookings."""
        expired = False
        events: list[DomainEvent] = []
        async with self._uow_factory() as uow:
            # --- LAYER 4: pessimistic row lock; Celery's expiry sweep and a concurrent
            # cancel queue behind us instead of racing.
            booking = await uow.bookings.get_for_update(booking_id)
            if booking is None:
                raise BookingNotFoundError(str(booking_id))
            if booking.status is BookingStatus.CONFIRMED:
                return booking

            now = self._clock()
            if (
                booking.status is BookingStatus.PENDING
                and booking.hold_expires_at is not None
                and booking.hold_expires_at <= now
            ):
                booking.transition_to(BookingStatus.EXPIRED)
                expired = True
            else:
                booking.transition_to(BookingStatus.CONFIRMED)  # raises if not PENDING
            await uow.bookings.save(booking)
            await uow.commit()
            events.append(_booking_event(booking, now))

        # The DB row (CONFIRMED / EXPIRED) now carries the exclusion guarantee, so the
        # Redis cart blocks are no longer needed.
        await self._release(booking.room_id, booking.slot, booking.id)
        await self._events.publish(events)
        if expired:
            raise HoldExpiredError("the 5-minute hold expired before confirmation")
        return booking

    # --------------------------------------------------------------- cancel
    async def cancel(self, booking_id: UUID) -> Booking:
        """PENDING | CONFIRMED -> CANCELLED. Idempotent for already cancelled bookings."""
        async with self._uow_factory() as uow:
            booking = await uow.bookings.get_for_update(booking_id)
            if booking is None:
                raise BookingNotFoundError(str(booking_id))
            if booking.status is BookingStatus.CANCELLED:
                return booking

            now = self._clock()
            booking.transition_to(BookingStatus.CANCELLED)
            await uow.bookings.save(booking)
            await uow.commit()
            event = _booking_event(booking, now)

        await self._release(booking.room_id, booking.slot, booking.id)
        await self._events.publish([event])
        return booking

    # ----------------------------------------------------------------- read
    async def get(self, booking_id: UUID) -> Booking:
        async with self._uow_factory() as uow:
            booking = await uow.bookings.get(booking_id)
        if booking is None:
            raise BookingNotFoundError(str(booking_id))
        return booking

    async def _release(self, room_id: UUID, slot: TimeSlot, owner: UUID) -> None:
        # The store already swallows Redis errors (TTL is the safety net).
        await self._holds.release(room_id, slot, owner)
