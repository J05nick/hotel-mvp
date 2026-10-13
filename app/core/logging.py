"""Structured logging to stdout.

12-Factor App (XI. Logs): the app never manages log files; it writes one JSON
event per line to stdout and lets the platform route/aggregate the stream.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final

# Standard LogRecord attributes (+ uvicorn's ANSI `color_message`) are not "extra" fields.
_RESERVED_ATTRS: Final[frozenset[str]] = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "color_message"}
)


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object.

    Anything passed through `extra={...}` is merged into the top-level object,
    so `logger.info("booking.created", extra={"booking_id": str(id)})` produces
    a queryable field instead of an interpolated string.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(
            {
                key: value
                for key, value in vars(record).items()
                if key not in _RESERVED_ATTRS and not key.startswith("_")
            }
        )
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Idempotently route root, uvicorn and celery loggers to a JSON stdout stream."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # Let framework loggers propagate to root so everything shares one format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "celery"):
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True
