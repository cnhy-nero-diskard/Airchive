"""Interval consumption from two cumulative readings.

Every branch here exists because the naive subtraction is wrong in a specific,
observable way: the counter resets at LG's midnight, the provider occasionally
revises a value downward, cycles get missed, and the first observation has
nothing to subtract from. Each of those produces a *classified* result — never a
plausible-looking wrong number, and never a fabricated one.

Arithmetic is `Decimal` throughout (design D4). `8.751 - 8.732` in IEEE-754 is
`0.019000000000000794`; stored for years, that noise is indistinguishable from
real measurement noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from airchive.observation.model import IntervalStatus, PreviousReading, QualityFlag

#: An interval longer than this multiple of the nominal cadence is "coarse".
COARSE_INTERVAL_FACTOR = 1.5


@dataclass
class IntervalOutcome:
    """The derived interval and the reason it is what it is."""

    interval_value: Decimal | None
    interval_seconds: float | None
    status: IntervalStatus
    flags: list[QualityFlag] = field(default_factory=list)
    final_previous_day_total: Decimal | None = None

    def add(self, flag: QualityFlag) -> None:
        if flag not in self.flags:
            self.flags.append(flag)


def quantize_like(value: Decimal, *references: Decimal | None) -> Decimal:
    """Round `value` to the precision of the source counter.

    The counter's precision is whatever the device actually reports, so it is
    taken from the readings themselves rather than assumed.
    """
    places = 0
    for reference in (*references, value):
        if reference is None:
            continue
        exponent = reference.as_tuple().exponent
        if isinstance(exponent, int) and exponent < 0:
            places = max(places, -exponent)
    if places == 0:
        return value
    return value.quantize(Decimal(1).scaleb(-places))


def _duration_seconds(previous_at: datetime, observed_at: datetime) -> float:
    return (observed_at - previous_at).total_seconds()


def compute_interval(
    *,
    current_value: Decimal | None,
    observed_at: datetime,
    local_date: date,
    previous: PreviousReading | None,
    has_prior_observation: bool,
    final_previous_day_total: Decimal | None = None,
    nominal_interval_seconds: int = 300,
) -> IntervalOutcome:
    """Classify and, where it is honest to do so, compute the interval.

    `previous` is the most recent *usable* prior reading, or `None`.
    `has_prior_observation` distinguishes "nothing has ever been recorded"
    (`NEW_BASELINE`) from "something was recorded but carries no usable energy"
    (`MISSING_PREVIOUS_SAMPLE`) — they look alike and mean different things
    about collector health.
    """
    if current_value is None:
        return IntervalOutcome(None, None, IntervalStatus.ENERGY_UNAVAILABLE)

    if previous is None:
        status = (
            IntervalStatus.MISSING_PREVIOUS_SAMPLE
            if has_prior_observation
            else IntervalStatus.NEW_BASELINE
        )
        return IntervalOutcome(None, None, status)

    duration = _duration_seconds(previous.observed_at, observed_at)
    day_gap = (local_date - previous.local_date).days

    if day_gap < 0:
        # The baseline belongs to a later day than the current reading: clock
        # skew or an out-of-order write. Refuse to subtract across it.
        return IntervalOutcome(None, duration, IntervalStatus.MISSING_PREVIOUS_SAMPLE)

    if day_gap == 0:
        return _same_day(current_value, previous, duration, nominal_interval_seconds)

    if day_gap == 1:
        return _day_rollover(current_value, previous, duration, final_previous_day_total)

    # More than one day boundary: reconstruction would need every intervening
    # day's finalized total, and the result would be meaningless anyway.
    return IntervalOutcome(None, duration, IntervalStatus.MULTI_DAY_GAP)


def _same_day(
    current_value: Decimal,
    previous: PreviousReading,
    duration: float,
    nominal_interval_seconds: int,
) -> IntervalOutcome:
    if current_value < previous.value:
        # A provider revision, not consumption. Never record a negative.
        return IntervalOutcome(None, duration, IntervalStatus.ANOMALOUS_DECREASE)

    interval = quantize_like(current_value - previous.value, current_value, previous.value)

    coarse = duration > nominal_interval_seconds * COARSE_INTERVAL_FACTOR
    status = IntervalStatus.COARSE_INTERVAL if coarse else IntervalStatus.NORMAL
    outcome = IntervalOutcome(interval, duration, status)

    if current_value == previous.value:
        # Genuine idleness or provider update latency — later analysis decides
        # which, and can only do so if we say the counter did not move.
        outcome.add(QualityFlag.UNCHANGED_COUNTER)
    return outcome


def _day_rollover(
    current_value: Decimal,
    previous: PreviousReading,
    duration: float,
    final_previous_day_total: Decimal | None,
) -> IntervalOutcome:
    """Reconstruct across midnight: (finalPrevious − previousDaily) + currentDaily."""
    if final_previous_day_total is None:
        # LG has not finalized yesterday yet. Defer; do not invent.
        return IntervalOutcome(None, duration, IntervalStatus.DAY_ROLLOVER_UNRESOLVED)

    if final_previous_day_total < previous.value:
        # The "final" total is below a value we already observed that day.
        outcome = IntervalOutcome(
            None,
            duration,
            IntervalStatus.DAY_ROLLOVER_UNRESOLVED,
            final_previous_day_total=final_previous_day_total,
        )
        outcome.add(QualityFlag.IMPLAUSIBLE_FINAL_TOTAL)
        return outcome

    remainder = final_previous_day_total - previous.value
    interval = quantize_like(
        remainder + current_value, current_value, previous.value, final_previous_day_total
    )
    return IntervalOutcome(
        interval,
        duration,
        IntervalStatus.DAY_ROLLOVER_RESOLVED,
        final_previous_day_total=final_previous_day_total,
    )
