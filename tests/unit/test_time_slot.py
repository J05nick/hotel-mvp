from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.domain.exceptions import InvalidTimeSlotError
from app.domain.value_objects import BLOCK, TimeSlot

T0 = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)


def test_valid_slot_counts_blocks() -> None:
    slot = TimeSlot(T0, T0 + timedelta(hours=2))
    assert slot.blocks == 4
    assert len(slot.iter_blocks()) == 4
    assert slot.iter_blocks()[-1].end == slot.end


def test_from_blocks() -> None:
    assert TimeSlot.from_blocks(T0, 3).end == T0 + BLOCK * 3


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (T0, T0),  # empty
        (T0, T0 - BLOCK),  # inverted
        (T0 + timedelta(minutes=15), T0 + timedelta(minutes=45)),  # misaligned
        (T0, T0 + timedelta(minutes=29)),  # shorter than a block
        (T0, T0 + BLOCK + timedelta(seconds=1)),  # sub-minute drift
        (T0.replace(tzinfo=None), (T0 + BLOCK).replace(tzinfo=None)),  # naive
    ],
)
def test_invalid_slots_are_rejected(start: datetime, end: datetime) -> None:
    with pytest.raises(InvalidTimeSlotError):
        TimeSlot(start, end)


def test_from_blocks_requires_at_least_one() -> None:
    with pytest.raises(InvalidTimeSlotError):
        TimeSlot.from_blocks(T0, 0)


def test_half_open_semantics() -> None:
    a = TimeSlot(T0, T0 + BLOCK)
    b = TimeSlot(T0 + BLOCK, T0 + 2 * BLOCK)
    c = TimeSlot(T0, T0 + 2 * BLOCK)
    assert not a.overlaps(b)  # touching edges are allowed back-to-back
    assert a.overlaps(c)
    assert c.overlaps(b)


def test_alignment_is_timezone_independent() -> None:
    madrid_summer = timezone(timedelta(hours=2))
    india = timezone(timedelta(hours=5, minutes=30))
    TimeSlot.from_blocks(datetime(2026, 10, 9, 12, 0, tzinfo=madrid_summer), 1)
    TimeSlot.from_blocks(datetime(2026, 10, 9, 15, 30, tzinfo=india), 1)
