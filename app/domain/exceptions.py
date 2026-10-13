"""Domain errors. Adapters translate these into HTTP / WebSocket responses."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for business-rule violations."""


class InvalidTimeSlotError(DomainError):
    """The requested range violates the 30-minute block rules."""


class SlotUnavailableError(DomainError):
    """The room is already held or booked for an overlapping range."""


class InvalidStateTransitionError(DomainError):
    """An entity was asked to move to a status not reachable from its current one."""


class NotFoundError(DomainError):
    """Base class for missing aggregates."""


class RoomNotFoundError(NotFoundError):
    pass


class BookingNotFoundError(NotFoundError):
    pass


class RoomNotBookableError(DomainError):
    """The room is out of service (e.g. under maintenance)."""


class HoldExpiredError(DomainError):
    """The 5-minute hold lapsed before the booking was confirmed."""


class IdempotencyKeyReuseError(DomainError):
    """An Idempotency-Key was replayed with different request parameters."""


class DuplicateIdempotencyKeyError(DomainError):
    """Persistence signal: another request already stored this Idempotency-Key."""


class ResourceBusyError(DomainError):
    """A database lock or statement timeout fired; the caller may retry."""
