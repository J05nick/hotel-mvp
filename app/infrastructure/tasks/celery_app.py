"""Celery application (broker & result backend: Redis) and the Beat schedule.

Run exactly ONE Beat process (it is a scheduler); scale workers freely.
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import setup_logging

from app.core.config import get_settings
from app.core.logging import configure_logging

_settings = get_settings()

celery_app = Celery(
    "hotel",
    broker=str(_settings.celery_broker_url),
    backend=str(_settings.celery_result_backend),
    # Import task modules when the worker starts (string => no import cycle).
    include=["app.infrastructure.tasks.housekeeping"],
)

celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Reliability: ack only after the task finishes, re-queue if a worker dies.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Redis broker: messages not acked within this window are redelivered.
    # Must exceed the longest task runtime.
    broker_transport_options={"visibility_timeout": 3_600},
    broker_connection_retry_on_startup=True,
    result_expires=3_600,
    task_time_limit=120,
    task_soft_time_limit=90,
    beat_schedule={
        # Every minute: finished bookings -> COMPLETED, room -> CLEANING, WebSocket event.
        "complete-finished-bookings": {
            "task": "housekeeping.complete_finished_bookings",
            "schedule": 60.0,
            "options": {"expires": 55},
        },
        # Every minute: PENDING bookings with a lapsed 5-minute hold -> EXPIRED.
        "expire-stale-holds": {
            "task": "housekeeping.expire_stale_holds",
            "schedule": 60.0,
            "options": {"expires": 55},
        },
    },
)


@setup_logging.connect
def _configure_celery_logging(**_: Any) -> None:
    # Prevent Celery from hijacking the root logger; keep the JSON stdout stream.
    configure_logging(_settings.log_level)
