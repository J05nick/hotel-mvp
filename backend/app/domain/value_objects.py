"""Value objects.

`TimeSlot` is the heart of the business: bookings are sold in fixed blocks of
30 minutes, aligned to the wall-clock half hour (hh:00 / hh:30, UTC epoch based),
and modelled as half-open intervals `[start, end)` so that consecutive bookings
(10:00-10:30 and 10:30-11:00) touch but never overlap.

The exact same rules are enforced again in PostgreSQL via CHECK constraints —
the domain gives fast, friendly errors; the database is the last line of defence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Self

from app.domain.exceptions import InvalidTimeSlotError

BLOCK_MINUTES: Final[int] = 30
BLOCK: Final[timedelta] = timedelta(minutes=BLOCK_MINUTES)
_BLOCK_SECONDS: Final[int] = BLOCK_MINUTES * 60
MAX_BLOCKS_PER_BOOKING: Final[int] = 48 * 14  # two weeks — guards against abuse / typos


def _is_block_aligned(moment: datetime) -> bool:
    # Epoch-based alignment is timezone independent for every zone whose UTC
    # offset is a multiple of 30 min. Mirrors the SQL CHECK constraint exactly.
    return moment.timestamp() % _BLOCK_SECONDS == 0


@dataclass(frozen=True, slots=True)
class TimeSlot:
    """Immutable half-open interval `[start, end)` made of whole 30-minute blocks."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise InvalidTimeSlotError("start and end must be timezone-aware datetimes")
        if self.end <= self.start:
            raise InvalidTimeSlotError("end must be strictly after start")
        if not (_is_block_aligned(self.start) and _is_block_aligned(self.end)):
            raise InvalidTimeSlotError(
                f"start and end must be aligned to {BLOCK_MINUTES}-minute boundaries"
            )
        if self.blocks > MAX_BLOCKS_PER_BOOKING:
            raise InvalidTimeSlotError(f"a booking cannot exceed {MAX_BLOCKS_PER_BOOKING} blocks")

    @classmethod
    def from_blocks(cls, start: datetime, blocks: int) -> Self:
        if blocks < 1:
            raise InvalidTimeSlotError("a booking needs at least one block")
        return cls(start=start, end=start + BLOCK * blocks)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def blocks(self) -> int:
        return int(self.duration / BLOCK)

    def overlaps(self, other: TimeSlot) -> bool:
        # Half-open semantics: touching edges do NOT overlap (same as tstzrange &&).
        return self.start < other.end and other.start < self.end

    def iter_blocks(self) -> list[TimeSlot]:
        """Split into atomic 30-minute blocks (used for per-block Redis locks)."""
        return [
            TimeSlot(self.start + BLOCK * i, self.start + BLOCK * (i + 1))
            for i in range(self.blocks)
        ]

    def as_utc(self) -> TimeSlot:
        return TimeSlot(self.start.astimezone(UTC), self.end.astimezone(UTC))
