"""Assembling one observation from two independent request outcomes.

Energy and state are related but independent (spec: energy-observation). A
failure of one never discards the other, and neither is ever filled in from a
previous observation — a carried-forward state value is indistinguishable from a
real one once it is a year old.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from airchive.observation.delta import compute_interval
from airchive.observation.model import (
    Observation,
    PreviousReading,
    Quality,
    QualityFlag,
    SourceOutcome,
)
from airchive.observation.state import normalize_state
from airchive.thinq.failures import FailureClass, ThinqFailure


@dataclass
class RequestResult:
    """The outcome of one of the two per-cycle requests."""

    ok: bool
    raw: Any = None
    failure: ThinqFailure | None = None
    #: Energy only: the reading extracted from `raw`.
    value: Decimal | None = None
    unit: str | None = None

    @classmethod
    def success(cls, raw: Any, value: Decimal | None = None, unit: str | None = None):
        return cls(ok=True, raw=raw, value=value, unit=unit)

    @classmethod
    def failed(cls, failure: ThinqFailure):
        return cls(ok=False, failure=failure)

    def to_outcome(self) -> SourceOutcome:
        if self.ok:
            return SourceOutcome(ok=True)
        failure = self.failure
        return SourceOutcome(
            ok=False,
            failure_class=str(failure.failure_class) if failure else None,
            error_code=failure.code if failure else None,
            error_name=failure.error_name if failure else None,
        )


def _condition_flags(*results: RequestResult) -> list[QualityFlag]:
    """Conditions that are true of the cycle regardless of the interval arithmetic."""
    flags: list[QualityFlag] = []
    classes = {r.failure.failure_class for r in results if r.failure is not None}

    if FailureClass.RATE_LIMITED in classes:
        flags.append(QualityFlag.RATE_LIMITED)
    if FailureClass.DEVICE_OFFLINE in classes:
        flags.append(QualityFlag.DEVICE_OFFLINE)

    outcomes = [r.ok for r in results]
    if any(outcomes) and not all(outcomes):
        flags.append(QualityFlag.PARTIAL_OBSERVATION)
    return flags


def build_observation(
    *,
    sample_id: str,
    device_id: str,
    scheduled_at: datetime,
    observed_at: datetime,
    local_date: date,
    timezone_name: str,
    energy_property: str,
    energy: RequestResult,
    state: RequestResult,
    previous: PreviousReading | None = None,
    has_prior_observation: bool = False,
    final_previous_day_total: Decimal | None = None,
    nominal_interval_seconds: int = 300,
    metadata_version: str | None = None,
    collector_version: str | None = None,
) -> Observation:
    """Build the observation, classifying rather than guessing at every branch."""
    outcome = compute_interval(
        current_value=energy.value if energy.ok else None,
        observed_at=observed_at,
        local_date=local_date,
        previous=previous,
        has_prior_observation=has_prior_observation,
        final_previous_day_total=final_previous_day_total,
        nominal_interval_seconds=nominal_interval_seconds,
    )

    quality = Quality(interval_status=outcome.status)
    for flag in (*outcome.flags, *_condition_flags(energy, state)):
        quality.add(flag)

    return Observation(
        sample_id=sample_id,
        device_id=device_id,
        scheduled_at=scheduled_at,
        observed_at=observed_at,
        local_date=local_date,
        timezone_name=timezone_name,
        quality=quality,
        energy_source=energy.to_outcome(),
        state_source=state.to_outcome(),
        energy_property=energy_property,
        raw_daily_total=energy.value if energy.ok else None,
        unit=energy.unit if energy.ok else None,
        interval_value=outcome.interval_value,
        interval_seconds=outcome.interval_seconds,
        previous=previous,
        final_previous_day_total=outcome.final_previous_day_total,
        # State is stored only when it was actually retrieved this cycle.
        state=normalize_state(state.raw) if state.ok else None,
        raw_energy=energy.raw if energy.ok else None,
        raw_state=state.raw if state.ok else None,
        metadata_version=metadata_version,
        collector_version=collector_version,
    )
