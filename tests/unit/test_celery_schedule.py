from __future__ import annotations

from app.infrastructure.tasks import housekeeping
from app.infrastructure.tasks.celery_app import celery_app


def test_beat_runs_both_sweeps_every_minute() -> None:
    schedule = celery_app.conf.beat_schedule
    by_task = {entry["task"]: entry for entry in schedule.values()}

    assert set(by_task) == {
        housekeeping.COMPLETE_FINISHED_BOOKINGS,
        housekeeping.EXPIRE_STALE_HOLDS,
    }
    for entry in by_task.values():
        assert entry["schedule"] == 60.0
        # A run queued while workers were down is dropped instead of piling up.
        assert entry["options"]["expires"] < 60


def test_scheduled_tasks_are_registered_in_the_worker() -> None:
    celery_app.loader.import_default_modules()
    for name in (housekeeping.COMPLETE_FINISHED_BOOKINGS, housekeeping.EXPIRE_STALE_HOLDS):
        assert name in celery_app.tasks


def test_tasks_are_reliable_and_bounded() -> None:
    task = celery_app.tasks[housekeeping.COMPLETE_FINISHED_BOOKINGS]
    assert task.acks_late is True
    assert task.time_limit is not None
    assert task.time_limit < 60  # must finish before the next tick
