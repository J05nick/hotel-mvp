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
