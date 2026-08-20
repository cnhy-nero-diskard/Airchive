"""Normalizing the device state payload.

"Normalize" here means *clean*, not *reshape*. The device's own vocabulary is
kept verbatim — every readable property this model exposes, nested exactly as
ThinQ returns it, which keeps `state.operation.airConOperationMode` and friends
directly queryable in Firestore through dotted paths.

Two rules: nothing absent is invented (no defaults, no placeholders), and
nothing present is narrowed away — including the temperature unit, which is
meaningless to guess and cheap to keep.
"""

from __future__ import annotations

from typing import Any

#: Keys worth surfacing to an operator at a glance, wherever they appear.
PRINCIPAL_KEYS = (
    "airConOperationMode",
    "currentJobMode",
    "currentTemperature",
    "targetTemperature",
    "unit",
    "windStrength",
    "powerSaveEnabled",
    "airCleanOperationMode",
)


def _clean(value: Any) -> Any:
    """Drop nulls and empty containers; keep everything else exactly as given."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            result = _clean(item)
            if result is not None:
                cleaned[key] = result
        return cleaned or None
    if isinstance(value, list):
        cleaned_list = [item for item in (_clean(v) for v in value) if item is not None]
        return cleaned_list or None
    return value


def normalize_state(payload: Any) -> dict[str, Any] | None:
    """Return the readable state as stored, or `None` when there is nothing to store.

    A payload of an unexpected shape yields `None` rather than a guess; the raw
    payload is retained on the observation regardless, so nothing is lost.
    """
    if not isinstance(payload, dict):
        return None
    return _clean(payload)


def principal_fields(state: Any, _depth: int = 0) -> dict[str, Any]:
    """Pull out the handful of fields an operator scans first.

    Purely a display convenience for the inspection commands — the stored
    document keeps the device's full structure.
    """
    found: dict[str, Any] = {}
    if _depth > 5:
        return found
    if isinstance(state, dict):
        for key, value in state.items():
            if key in PRINCIPAL_KEYS and not isinstance(value, dict | list):
                found.setdefault(key, value)
            else:
                for nested_key, nested_value in principal_fields(value, _depth + 1).items():
                    found.setdefault(nested_key, nested_value)
    elif isinstance(state, list):
        for item in state:
            for nested_key, nested_value in principal_fields(item, _depth + 1).items():
                found.setdefault(nested_key, nested_value)
    return found
