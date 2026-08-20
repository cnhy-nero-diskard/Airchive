"""A last line of defence against secrets reaching output.

The primary mechanism is structural: the SDK boundary (`airchive.thinq.client`)
never lets an exception carrying request headers escape. This module is the belt
to that suspenders — any string on its way to a log record, a CLI print, or a
persisted document can be scrubbed of registered secret values first.
"""

from __future__ import annotations

from typing import Any

REDACTED = "<redacted>"

# Shorter values would cause collateral scrubbing of ordinary text.
_MIN_SECRET_LENGTH = 8

_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a value that must never appear in output."""
    if value and len(value) >= _MIN_SECRET_LENGTH:
        _secrets.add(value)


def clear_secrets() -> None:
    """Forget every registered secret (tests only)."""
    _secrets.clear()


def scrub(text: str) -> str:
    """Replace every registered secret occurring in `text`."""
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def scrub_object(value: Any) -> Any:
    """Recursively scrub strings inside dicts, lists, and tuples."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: scrub_object(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_object(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_object(v) for v in value)
    return value
