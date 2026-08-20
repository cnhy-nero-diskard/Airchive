"""The shape of one observation, and the quality vocabulary it carries.

Quality is three orthogonal things (design D5), never one overloaded field:

* `quality.intervalStatus` — exactly one value, explaining why `intervalValue`
  is what it is.
* `quality.flags` — zero or more independent conditions that can coexist. An
  array so `array-contains` makes each one queryable on its own.
* `source.energy` / `source.state` — the outcome of each request, recorded
  separately from the interval, because a failed source is a property of the
  source and not of the arithmetic.

Exact decimal values are stored as strings, which is what makes them exact; a
float mirror is stored alongside each one purely so ranges and aggregates are
queryable. The strings are authoritative and every derived number is
recomputable from them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class IntervalStatus(StrEnum):
    """Why `intervalValue` holds what it holds. Exactly one applies."""

    NORMAL = "NORMAL"
    NEW_BASELINE = "NEW_BASELINE"
    MISSING_PREVIOUS_SAMPLE = "MISSING_PREVIOUS_SAMPLE"
    COARSE_INTERVAL = "COARSE_INTERVAL"
    DAY_ROLLOVER_RESOLVED = "DAY_ROLLOVER_RESOLVED"
    DAY_ROLLOVER_UNRESOLVED = "DAY_ROLLOVER_UNRESOLVED"
    ANOMALOUS_DECREASE = "ANOMALOUS_DECREASE"
    MULTI_DAY_GAP = "MULTI_DAY_GAP"
    ENERGY_UNAVAILABLE = "ENERGY_UNAVAILABLE"


class QualityFlag(StrEnum):
    """Independent conditions. Any number may apply at once."""

    RATE_LIMITED = "RATE_LIMITED"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    UNCHANGED_COUNTER = "UNCHANGED_COUNTER"
    PARTIAL_OBSERVATION = "PARTIAL_OBSERVATION"
    RECONCILED = "RECONCILED"
    IMPLAUSIBLE_FINAL_TOTAL = "IMPLAUSIBLE_FINAL_TOTAL"


#: Statuses and flags that mark an observation as worth an operator's attention.
ANOMALOUS_STATUSES = frozenset(
    {
        IntervalStatus.ANOMALOUS_DECREASE,
        IntervalStatus.DAY_ROLLOVER_UNRESOLVED,
        IntervalStatus.MULTI_DAY_GAP,
        IntervalStatus.MISSING_PREVIOUS_SAMPLE,
        IntervalStatus.ENERGY_UNAVAILABLE,
    }
)

ANOMALOUS_FLAGS = frozenset(
    {
        QualityFlag.DEVICE_OFFLINE,
        QualityFlag.RATE_LIMITED,
        QualityFlag.PARTIAL_OBSERVATION,
        QualityFlag.IMPLAUSIBLE_FINAL_TOTAL,
    }
)


@dataclass(frozen=True)
class SourceOutcome:
    """How one of the two requests went. Never encoded into the interval status."""

    ok: bool
    failure_class: str | None = None
    error_code: str | None = None
    error_name: str | None = None

    def to_document(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failureClass": self.failure_class,
            "errorCode": self.error_code,
            "errorName": self.error_name,
        }


@dataclass
class Quality:
    interval_status: IntervalStatus
    flags: list[QualityFlag] = field(default_factory=list)

    def add(self, flag: QualityFlag) -> None:
        if flag not in self.flags:
            self.flags.append(flag)

    def has(self, flag: QualityFlag) -> bool:
        return flag in self.flags

    @property
    def is_anomalous(self) -> bool:
        return self.interval_status in ANOMALOUS_STATUSES or any(
            flag in ANOMALOUS_FLAGS for flag in self.flags
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "intervalStatus": str(self.interval_status),
            "flags": [str(flag) for flag in self.flags],
        }


@dataclass(frozen=True)
class PreviousReading:
    """The baseline an interval was computed against."""

    sample_id: str
    observed_at: datetime
    local_date: date
    value: Decimal

    def to_document(self) -> dict[str, Any]:
        return {
            "sampleId": self.sample_id,
            "observedAt": self.observed_at,
            "localDate": self.local_date.isoformat(),
            **decimal_fields("rawDailyTotal", self.value),
        }


def decimal_fields(name: str, value: Decimal | None) -> dict[str, Any]:
    """Store a decimal exactly (string) plus a float mirror for querying.

    Firestore has no decimal type. The string is authoritative — it is what
    later analysis re-derives from — and the `…Number` mirror exists so range
    queries and aggregations work without parsing every document.
    """
    if value is None:
        return {name: None, f"{name}Number": None}
    return {name: str(value), f"{name}Number": float(value)}


@dataclass
class Observation:
    """One sampling occasion. Immutable once written, apart from reconciliation."""

    sample_id: str
    device_id: str
    scheduled_at: datetime
    observed_at: datetime
    local_date: date
    timezone_name: str
    quality: Quality
    energy_source: SourceOutcome
    state_source: SourceOutcome
    energy_property: str | None = None
    raw_daily_total: Decimal | None = None
    unit: str | None = None
    interval_value: Decimal | None = None
    interval_seconds: float | None = None
    previous: PreviousReading | None = None
    final_previous_day_total: Decimal | None = None
    state: dict[str, Any] | None = None
    raw_energy: Any = None
    raw_state: Any = None
    metadata_version: str | None = None
    collector_version: str | None = None
    persisted_at: datetime | None = None
    reconciled_at: datetime | None = None

    @property
    def completeness(self) -> int:
        """Precedence score for idempotent writes (design D7).

        Energy outranks state: an observation without energy cannot serve as a
        baseline for the next interval, so losing it costs more.
        """
        return (2 if self.energy_source.ok else 0) + (1 if self.state_source.ok else 0)

    @property
    def has_usable_energy(self) -> bool:
        return self.energy_source.ok and self.raw_daily_total is not None

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "sampleId": self.sample_id,
            "deviceId": self.device_id,
            "scheduledAt": self.scheduled_at,
            "observedAt": self.observed_at,
            "persistedAt": self.persisted_at,
            "localDate": self.local_date.isoformat(),
            "timezone": self.timezone_name,
            "completeness": self.completeness,
            "energy": {
                "property": self.energy_property,
                "unit": self.unit,
                **decimal_fields("rawDailyTotal", self.raw_daily_total),
                **decimal_fields("intervalValue", self.interval_value),
                "intervalSeconds": self.interval_seconds,
                "previous": self.previous.to_document() if self.previous else None,
                **decimal_fields("finalPreviousDayTotal", self.final_previous_day_total),
            },
            "quality": self.quality.to_document(),
            "source": {
                "energy": self.energy_source.to_document(),
                "state": self.state_source.to_document(),
            },
            "state": self.state,
            "raw": {"energy": self.raw_energy, "state": self.raw_state},
            "metadataVersion": self.metadata_version,
            "collectorVersion": self.collector_version,
        }
        if self.reconciled_at is not None:
            document["reconciledAt"] = self.reconciled_at
        return document
