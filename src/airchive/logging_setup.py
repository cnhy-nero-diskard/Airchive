"""Structured logging, correlated to the record each cycle produced.

One line of JSON per event, carrying the sample identifier so a stored
observation can always be traced back to the logs that produced it — and vice
versa. Every rendered value passes through the secret scrubber on the way out,
because the cost of one leaked token in retained logs is not recoverable.

Operational logging goes here; telemetry goes to Firestore. Neither substitutes
for the other.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

LOGGER_NAME = "airchive"

#: Key under which cycle context is attached to a log record.
CONTEXT_KEY = "airchive"

_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """Render a record as one scrubbed JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        from airchive.redaction import scrub, scrub_object

        payload: dict[str, Any] = {
            "severity": record.levelname,
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }

        context = getattr(record, CONTEXT_KEY, None)
        if isinstance(context, dict):
            payload.update(scrub_object(context))

        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != CONTEXT_KEY and not key.startswith("_"):
                payload[key] = scrub_object(value)

        if record.exc_info:
            # Formatted, then scrubbed — never the exception object itself.
            payload["exception"] = scrub(self.formatException(record.exc_info))

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", stream=None) -> logging.Logger:
    """Install the JSON handler on the `airchive` logger and return it."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")
