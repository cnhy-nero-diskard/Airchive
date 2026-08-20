"""Interval semantics (spec: energy-observation).

Every scenario the spec spells out with concrete numbers is asserted here with
those numbers.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from airchive.observation.delta import compute_interval, quantize_like
from airchive.observation.model import IntervalStatus, PreviousReading, QualityFlag

MANILA = ZoneInfo("Asia/Manila")


def at(hour: int, minute: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=MANILA)


def previous_reading(
    value: str, observed_at: datetime, local_date: date | None = None
) -> PreviousReading:
    return PreviousReading(
        sample_id="prev",
        observed_at=observed_at,
        local_date=local_date or observed_at.date(),
        value=Decimal(value),
    )


def compute(current: str | None, previous: PreviousReading | None, **kwargs):
    kwargs.setdefault("observed_at", at(12, 5))
    kwargs.setdefault("local_date", kwargs["observed_at"].date())
    kwargs.setdefault("has_prior_observation", previous is not None)
    return compute_interval(
        current_value=Decimal(current) if current is not None else None,
        previous=previous,
        **kwargs,
    )


# --- Same-day arithmetic -----------------------------------------------------


def test_normal_same_day_increment():
    outcome = compute("2.150", previous_reading("2.100", at(12, 0)))

    assert outcome.interval_value == Decimal("0.050")
    assert outcome.status is IntervalStatus.NORMAL
    assert outcome.flags == []
    assert outcome.interval_seconds == 300


def test_unchanged_counter_is_zero_and_flagged_not_discarded():
    outcome = compute("2.100", previous_reading("2.100", at(12, 0)))

    assert outcome.interval_value == Decimal("0.000")
    assert outcome.status is IntervalStatus.NORMAL
    assert QualityFlag.UNCHANGED_COUNTER in outcome.flags


def test_decimal_arithmetic_introduces_no_float_artifact():
    outcome = compute("8.751", previous_reading("8.732", at(12, 0)))

    assert outcome.interval_value == Decimal("0.019")
    assert str(outcome.interval_value) == "0.019"
    # The float path would give 0.019000000000000794.
    assert outcome.interval_value != Decimal(str(8.751 - 8.732))


def test_result_is_quantized_to_the_precision_of_the_source_counter():
    assert quantize_like(Decimal("0.0190000"), Decimal("8.751"), Decimal("8.732")) == Decimal(
        "0.019"
    )
    assert str(quantize_like(Decimal("1"), Decimal("2.10"))) == "1.00"


# --- Duration ----------------------------------------------------------------


def test_duration_comes_from_observation_times_not_the_configured_cadence():
    outcome = compute(
        "2.190", previous_reading("2.100", at(12, 0)), observed_at=at(12, 15)
    )

    assert outcome.interval_seconds == pytest.approx(900)
    assert outcome.interval_seconds != 300


def test_missed_cycles_give_a_coarse_but_valid_interval():
    outcome = compute(
        "2.190", previous_reading("2.100", at(12, 0)), observed_at=at(12, 15)
    )

    assert outcome.interval_value == Decimal("0.090")
    assert outcome.interval_seconds == pytest.approx(900)
    assert outcome.status is IntervalStatus.COARSE_INTERVAL


def test_a_slightly_late_cycle_is_still_normal():
    outcome = compute(
        "2.150", previous_reading("2.100", at(12, 0)), observed_at=at(12, 5) + timedelta(seconds=60)
    )

    assert outcome.status is IntervalStatus.NORMAL


# --- Baselines ---------------------------------------------------------------


def test_first_ever_observation_is_a_baseline_not_consumption():
    outcome = compute("2.100", None, has_prior_observation=False)

    assert outcome.interval_value is None
    assert outcome.status is IntervalStatus.NEW_BASELINE
    assert outcome.interval_seconds is None


def test_a_prior_observation_without_usable_energy_is_distinct_from_a_new_baseline():
    outcome = compute("2.100", None, has_prior_observation=True)

    assert outcome.interval_value is None
    assert outcome.status is IntervalStatus.MISSING_PREVIOUS_SAMPLE


# --- Anomalous decrease ------------------------------------------------------


def test_same_day_decrease_never_yields_a_negative_interval():
    outcome = compute("3.200", previous_reading("3.500", at(12, 0)))

    assert outcome.interval_value is None
    assert outcome.status is IntervalStatus.ANOMALOUS_DECREASE
    assert outcome.interval_seconds == 300


# --- Day rollover ------------------------------------------------------------


def test_rollover_never_subtracts_across_midnight():
    outcome = compute(
        "0.021",
        previous_reading("8.732", at(23, 55, day=19)),
        observed_at=at(0, 5, day=20),
    )

    assert outcome.interval_value is None or outcome.interval_value >= 0
    assert outcome.status is not IntervalStatus.NORMAL
    assert outcome.status is IntervalStatus.DAY_ROLLOVER_UNRESOLVED


def test_cross_midnight_reconstruction_from_the_finalized_previous_day():
    outcome = compute(
        "0.021",
        previous_reading("8.732", at(23, 55, day=19)),
        observed_at=at(0, 5, day=20),
        final_previous_day_total=Decimal("8.751"),
    )

    # (8.751 - 8.732) + 0.021
    assert outcome.interval_value == Decimal("0.040")
    assert outcome.status is IntervalStatus.DAY_ROLLOVER_RESOLVED
    assert outcome.final_previous_day_total == Decimal("8.751")


def test_unavailable_finalized_total_defers_rather_than_invents():
    outcome = compute(
        "0.021",
        previous_reading("8.732", at(23, 55, day=19)),
        observed_at=at(0, 5, day=20),
        final_previous_day_total=None,
    )

    assert outcome.interval_value is None
    assert outcome.status is IntervalStatus.DAY_ROLLOVER_UNRESOLVED
    assert QualityFlag.IMPLAUSIBLE_FINAL_TOTAL not in outcome.flags


def test_implausible_finalized_total_is_refused_and_recorded():
    outcome = compute(
        "0.021",
        previous_reading("8.732", at(23, 55, day=19)),
        observed_at=at(0, 5, day=20),
        final_previous_day_total=Decimal("8.500"),
    )

    assert outcome.interval_value is None
    assert outcome.status is IntervalStatus.DAY_ROLLOVER_UNRESOLVED
    assert QualityFlag.IMPLAUSIBLE_FINAL_TOTAL in outcome.flags
    assert outcome.final_previous_day_total == Decimal("8.500")


def test_more_than_one_day_of_gap_is_not_reconstructed():
    outcome = compute(
        "0.021",
        previous_reading("8.732", at(23, 55, day=17)),
        observed_at=at(0, 5, day=20),
        final_previous_day_total=Decimal("9.000"),
    )

    assert outcome.interval_value is None
    assert outcome.status is IntervalStatus.MULTI_DAY_GAP


def test_a_baseline_from_a_later_day_is_refused():
    outcome = compute(
        "0.021",
        previous_reading("8.732", at(12, 0, day=21)),
        observed_at=at(12, 5, day=20),
    )

    assert outcome.interval_value is None
    assert outcome.status is IntervalStatus.MISSING_PREVIOUS_SAMPLE


# --- Energy unavailable ------------------------------------------------------


def test_a_failed_energy_request_yields_no_interval():
    outcome = compute(None, previous_reading("2.100", at(12, 0)))

    assert outcome.interval_value is None
    assert outcome.status is IntervalStatus.ENERGY_UNAVAILABLE
