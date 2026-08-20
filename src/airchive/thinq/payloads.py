"""Reading what ThinQ actually returns, defensively.

The exact shape of the energy-usage response is an open question the discovery
phase answers against the real device (design — Open Questions). Until then the
extractor searches the payload for a numeric reading under the names ThinQ
plausibly uses, and reports failure rather than guessing when it finds none.
Nothing here fabricates a value; the raw payload is always preserved by the
caller regardless of what is extracted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

AIR_CONDITIONER_TYPE = "DEVICE_AIR_CONDITIONER"

#: Keys that have been seen (or are plausible) as the numeric usage value.
_VALUE_KEYS = (
    "value",
    "usage",
    "consumption",
    "energyUsage",
    "energyConsumption",
    "totalUsage",
    "total",
    "amount",
)

#: Keys that carry the unit of measure.
_UNIT_KEYS = ("unit", "unitOfMeasure", "energyUnit", "usageUnit", "measureUnit")

#: Keys whose values are lists of per-period records.
_LIST_KEYS = ("energyData", "usageData", "data", "usageList", "items", "list", "energyUsage")

#: Keys that carry the period's date.
_DATE_KEYS = ("date", "startDate", "usageDate", "day", "period")


@dataclass(frozen=True)
class DeviceCandidate:
    """One entry from the device list."""

    device_id: str
    alias: str | None
    model_name: str | None
    device_type: str | None
    raw: dict[str, Any]

    @property
    def is_air_conditioner(self) -> bool:
        return self.device_type == AIR_CONDITIONER_TYPE


@dataclass(frozen=True)
class EnergyReading:
    """A cumulative reading pulled out of an energy-usage response."""

    value: Decimal
    unit: str | None
    date_label: str | None

    @property
    def decimal_places(self) -> int:
        exponent = self.value.as_tuple().exponent
        return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def parse_device_list(payload: Any) -> list[DeviceCandidate]:
    """Turn a device-list response into candidates. Unknown shapes yield nothing."""
    if not isinstance(payload, list):
        return []

    candidates: list[DeviceCandidate] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        device_id = entry.get("deviceId")
        if not device_id:
            continue
        info = entry.get("deviceInfo") or {}
        candidates.append(
            DeviceCandidate(
                device_id=str(device_id),
                alias=info.get("alias"),
                model_name=info.get("modelName"),
                device_type=info.get("deviceType"),
                raw=entry,
            )
        )
    return candidates


def supported_energy_properties(energy_profile: Any) -> list[str]:
    """Read `energy_profile["result"]["property"]` — the device's own answer."""
    if not isinstance(energy_profile, dict):
        return []
    result = energy_profile.get("result")
    if not isinstance(result, dict):
        return []
    properties = result.get("property")
    if isinstance(properties, str):
        return [properties]
    if isinstance(properties, list):
        return [str(p) for p in properties]
    if isinstance(properties, dict):
        return [str(key) for key in properties]
    return []


def _to_decimal(value: Any) -> Decimal | None:
    """Convert a scalar to Decimal without ever routing through binary float."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Decimal(str(x)) keeps the decimal digits the JSON actually carried.
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def _find_unit(node: Any, depth: int = 0) -> str | None:
    if depth > 6:
        return None
    if isinstance(node, dict):
        for key in _UNIT_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in node.values():
            found = _find_unit(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_unit(item, depth + 1)
            if found:
                return found
    return None


def _record_value(record: dict[str, Any], energy_property: str) -> Decimal | None:
    for key in (energy_property, *_VALUE_KEYS):
        if key in record:
            value = _to_decimal(record[key])
            if value is not None:
                return value
    return None


def _record_date(record: dict[str, Any]) -> str | None:
    for key in _DATE_KEYS:
        value = record.get(key)
        if isinstance(value, str | int) and str(value).strip():
            return str(value).strip()
    return None


def extract_energy_reading(
    payload: Any, energy_property: str, *, day_label: str | None = None
) -> EnergyReading | None:
    """Pull the cumulative reading for `day_label` (or the last record) out of `payload`.

    Returns `None` when no numeric reading can be located — the caller treats
    that as a malformed response rather than as a zero.
    """
    if payload is None:
        return None

    node: Any = payload
    if isinstance(node, dict) and isinstance(node.get("result"), dict | list):
        node = node["result"]

    unit = _find_unit(payload)

    records: list[dict[str, Any]] = []
    if isinstance(node, list):
        records = [r for r in node if isinstance(r, dict)]
    elif isinstance(node, dict):
        for key in _LIST_KEYS:
            value = node.get(key)
            if isinstance(value, list):
                records = [r for r in value if isinstance(r, dict)]
                break
        else:
            value = _record_value(node, energy_property)
            if value is not None:
                return EnergyReading(value=value, unit=unit, date_label=_record_date(node))
            # A nested single object, e.g. {"result": {"energyConsumption": {...}}}
            nested = node.get(energy_property)
            if isinstance(nested, dict):
                value = _record_value(nested, energy_property)
                if value is not None:
                    return EnergyReading(
                        value=value,
                        unit=_find_unit(nested) or unit,
                        date_label=_record_date(nested),
                    )
            return None

    if not records:
        return None

    chosen: dict[str, Any] | None = None
    if day_label:
        wanted = day_label.replace("-", "")
        for record in records:
            label = _record_date(record)
            if label and label.replace("-", "") == wanted:
                chosen = record
                break
    if chosen is None:
        chosen = records[-1]

    value = _record_value(chosen, energy_property)
    if value is None:
        return None
    return EnergyReading(
        value=value,
        unit=_find_unit(chosen) or unit,
        date_label=_record_date(chosen),
    )
