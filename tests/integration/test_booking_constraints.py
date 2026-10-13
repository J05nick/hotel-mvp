"""Prove the database-level guarantees of the schema."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from psycopg import errors as pg_errors
from sqlalchemy import Engine, select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.domain.enums import BookingStatus, RoomStatus
from app.infrastructure.db.models import BOOKING_OVERLAP_CONSTRAINT, BookingModel, RoomModel

pytestmark = pytest.mark.integration

T0 = datetime(2030, 1, 1, 10, 0, tzinfo=UTC)
HALF = timedelta(minutes=30)


def _room(session: Session) -> RoomModel:
    room = RoomModel(
        number=uuid.uuid4().hex[:8], name="Test", capacity=2, price_per_block_cents=1_000
    )
    session.add(room)
    session.flush()
    return room


def _booking(
    room_id: uuid.UUID,
    start: datetime,
    end: datetime,
    status: BookingStatus = BookingStatus.CONFIRMED,
) -> BookingModel:
    return BookingModel(
        room_id=room_id,
        guest_name="Guest",
        guest_email="guest@example.com",
        during=Range(start, end, bounds="[)"),
        status=status,
        total_price_cents=1_000,
    )


def test_overlapping_active_bookings_are_rejected(engine: Engine) -> None:
    with Session(engine) as s:
        room = _room(s)
        s.add(_booking(room.id, T0, T0 + 2 * HALF))
        s.commit()

        s.add(_booking(room.id, T0 + HALF, T0 + 3 * HALF))
        with pytest.raises(IntegrityError) as exc_info:
            s.commit()
        assert isinstance(exc_info.value.orig, pg_errors.ExclusionViolation)
        assert BOOKING_OVERLAP_CONSTRAINT in str(exc_info.value.orig)


def test_back_to_back_and_other_rooms_are_allowed(engine: Engine) -> None:
    with Session(engine) as s:
        room_a, room_b = _room(s), _room(s)
        s.add_all(
            [
                _booking(room_a.id, T0, T0 + HALF),
                _booking(room_a.id, T0 + HALF, T0 + 2 * HALF),  # touches, [) bounds
                _booking(room_b.id, T0, T0 + 2 * HALF),  # same time, other room
            ]
        )
        s.commit()


@pytest.mark.parametrize(
    "released", [BookingStatus.CANCELLED, BookingStatus.EXPIRED, BookingStatus.COMPLETED]
)
def test_terminal_status_releases_range(engine: Engine, released: BookingStatus) -> None:
    with Session(engine) as s:
        room = _room(s)
        s.add(_booking(room.id, T0, T0 + HALF, status=released))
        s.add(_booking(room.id, T0, T0 + HALF, status=BookingStatus.PENDING))
        s.commit()


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (T0 + timedelta(minutes=10), T0 + timedelta(minutes=40)),  # misaligned
        (T0, T0 + timedelta(minutes=45)),  # not a multiple of 30
        (T0, T0 + HALF + timedelta(milliseconds=1)),  # sub-second drift
    ],
)
def test_block_alignment_check(engine: Engine, start: datetime, end: datetime) -> None:
    with Session(engine) as s:
        room = _room(s)
        s.add(_booking(room.id, start, end))
        with pytest.raises(IntegrityError) as exc_info:
            s.commit()
        assert isinstance(exc_info.value.orig, pg_errors.CheckViolation)


def test_concurrent_inserts_only_one_wins(engine: Engine) -> None:
    """N threads race to book the same slot; PostgreSQL lets exactly one commit."""
    with Session(engine) as s:
        room_id = _room(s).id
        s.commit()

    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(offset: int) -> None:
        # Different but overlapping ranges, to exercise && rather than equality.
        start = T0 + timedelta(days=1) + (HALF if offset % 2 else timedelta(0))
        with Session(engine) as session:
            session.add(_booking(room_id, start, start + 2 * HALF))
            barrier.wait()
            try:
                session.commit()
                result = "ok"
            except IntegrityError as exc:
                is_overlap = isinstance(exc.orig, pg_errors.ExclusionViolation)
                result = "conflict" if is_overlap else f"unexpected: {exc.orig!r}"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == workers - 1


def test_optimistic_locking_detects_lost_update(engine: Engine) -> None:
    with Session(engine) as s:
        room_id = _room(s).id
        s.commit()

    with Session(engine) as api, Session(engine) as worker:
        room_api = api.get_one(RoomModel, room_id)
        room_worker = worker.get_one(RoomModel, room_id)

        room_worker.status = RoomStatus.CLEANING
        worker.commit()  # version 1 -> 2

        room_api.status = RoomStatus.OCCUPIED  # still believes version 1
        with pytest.raises(StaleDataError):
            api.commit()

    with Session(engine) as s:
        room = s.scalars(select(RoomModel).where(RoomModel.id == room_id)).one()
        assert room.status is RoomStatus.CLEANING
        assert room.version_id == 2


def test_constraint_names_are_stable(engine: Engine) -> None:
    """Adapters map violations by constraint name — the names are part of the contract."""
    with engine.connect() as conn:
        names = set(
            conn.scalars(
                text("SELECT conname FROM pg_constraint WHERE conrelid = 'bookings'::regclass")
            )
        )
    assert names == {
        "pk_bookings",
        "fk_bookings_room_id_rooms",
        "uq_bookings_idempotency_key",
        BOOKING_OVERLAP_CONSTRAINT,
        "ck_bookings_during_bounded",
        "ck_bookings_during_half_open",
        "ck_bookings_during_block_aligned",
        "ck_bookings_total_price_non_negative",
        "ck_bookings_hold_only_when_pending",
    }


def test_models_match_migrations(alembic_cfg: Config) -> None:
    """`alembic check` fails if the ORM models drift from the migration history."""
    command.check(alembic_cfg)


def test_downgrade_and_upgrade_roundtrip(alembic_cfg: Config) -> None:
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
