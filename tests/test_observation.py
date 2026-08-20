"""The observation record: quality model, partial success, state preservation."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from airchive.observation.build import RequestResult, build_observation
from airchive.observation.model import (
    IntervalStatus,
    Observation,
    PreviousReading,
    Quality,
    QualityFlag,
    SourceOutcome,
)
from airchive.observation.state import normalize_state, principal_fields
from airchive.thinq.failures import FailureClass, ThinqFailure

MANILA = ZoneInfo("Asia/Manila")

STATE_PAYLOAD = {
    "airConJobMode": {"currentJobMode": "COOL"},
    "operation": {"airConOperationMode": "POWER_ON", "airCleanOperationMode": None},
    "temperature": {
        "currentTemperature": 27,
        "targetTemperature": 24,
        "unit": "C",
        "twoSetEnabled": None,
    },
    "airFlow": {"windStrength": "MID"},
    "powerSave": {"powerSaveEnabled": False},
    "filterInfo": {"usedTime": 120, "filterLifetime": 1000},
    "emptyGroup": {},
}

ENERGY_PAYLOAD = {"unit": "kWh", "energyData": [{"date": "20260820", "value": "2.150"}]}


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 20, hour, minute, tzinfo=MANILA)


def build(energy: RequestResult, state: RequestResult, **kwargs) -> Observation:
    kwargs.setdefault(
        "previous",
        PreviousReading("prev", at(12, 0), at(12, 0).date(), Decimal("2.100")),
    )
    kwargs.setdefault("has_prior_observation", True)
    return build_observation(
        sample_id="20260820T040500Z",
        device_id="ac-device-1",
        scheduled_at=at(12, 5),
        observed_at=at(12, 5),
        local_date=at(12, 5).date(),
        timezone_name="Asia/Manila",
        energy_property="energyConsumption",
        energy=energy,
        state=state,
        collector_version="0.1.0",
        **kwargs,
    )


def energy_ok(value: str = "2.150") -> RequestResult:
    return RequestResult.success(ENERGY_PAYLOAD, value=Decimal(value), unit="kWh")


def state_ok() -> RequestResult:
    return RequestResult.success(STATE_PAYLOAD)


def failed(failure_class: FailureClass, code: str | None = None) -> RequestResult:
    return RequestResult.failed(ThinqFailure(failure_class, code=code, error_name="X"))


# --- Quality model -----------------------------------------------------------


def test_independent_conditions_coexist_without_displacing_each_other():
    quality = Quality(interval_status=IntervalStatus.COARSE_INTERVAL)
    quality.add(QualityFlag.RATE_LIMITED)
    quality.add(QualityFlag.UNCHANGED_COUNTER)

    document = quality.to_document()
    assert document["intervalStatus"] == "COARSE_INTERVAL"
    assert set(document["flags"]) == {"RATE_LIMITED", "UNCHANGED_COUNTER"}

    # Neither flag displaced the other, and neither displaced the status.
    quality.add(QualityFlag.RATE_LIMITED)
    assert document["flags"].count("RATE_LIMITED") == 1


def test_a_rate_limited_coarse_interval_records_both_facts():
    observation = build(
        energy_ok("2.190"),
        failed(FailureClass.RATE_LIMITED, "1306"),
        previous=PreviousReading("prev", at(11, 50), at(11, 50).date(), Decimal("2.100")),
    )

    document = observation.to_document()
    assert document["quality"]["intervalStatus"] == "COARSE_INTERVAL"
    assert "RATE_LIMITED" in document["quality"]["flags"]
    assert "PARTIAL_OBSERVATION" in document["quality"]["flags"]


def test_source_outcomes_are_recorded_apart_from_the_interval_status():
    observation = build(energy_ok(), failed(FailureClass.DEVICE_OFFLINE, "1222"))
    document = observation.to_document()

    assert document["source"]["energy"] == {
        "ok": True,
        "failureClass": None,
        "errorCode": None,
        "errorName": None,
    }
    assert document["source"]["state"]["ok"] is False
    assert document["source"]["state"]["failureClass"] == "DEVICE_OFFLINE"
    assert document["source"]["state"]["errorCode"] == "1222"
    # The interval status says nothing about which source failed.
    assert document["quality"]["intervalStatus"] == "NORMAL"
    assert "DEVICE_OFFLINE" in document["quality"]["flags"]


def test_anomaly_detection_covers_statuses_and_flags():
    assert Quality(IntervalStatus.ANOMALOUS_DECREASE).is_anomalous
    assert Quality(IntervalStatus.DAY_ROLLOVER_UNRESOLVED).is_anomalous
    assert not Quality(IntervalStatus.NORMAL).is_anomalous

    quality = Quality(IntervalStatus.NORMAL)
    quality.add(QualityFlag.DEVICE_OFFLINE)
    assert quality.is_anomalous


# --- Partial success ---------------------------------------------------------


def test_energy_succeeds_and_state_fails():
    observation = build(energy_ok(), failed(FailureClass.TRANSPORT))
    document = observation.to_document()

    assert document["energy"]["rawDailyTotal"] == "2.150"
    assert document["energy"]["intervalValue"] == "0.050"
    assert document["state"] is None
    assert document["raw"]["state"] is None
    assert document["source"]["energy"]["ok"] is True
    assert document["source"]["state"]["ok"] is False
    assert "PARTIAL_OBSERVATION" in document["quality"]["flags"]
    assert observation.completeness == 2


def test_state_succeeds_and_energy_fails():
    observation = build(failed(FailureClass.TRANSIENT, "2000"), state_ok())
    document = observation.to_document()

    assert document["energy"]["rawDailyTotal"] is None
    assert document["energy"]["intervalValue"] is None
    assert document["quality"]["intervalStatus"] == "ENERGY_UNAVAILABLE"
    assert document["state"]["operation"]["airConOperationMode"] == "POWER_ON"
    assert observation.completeness == 1
    # It must not serve as a baseline for the next interval.
    assert not observation.has_usable_energy


def test_both_fail_and_nothing_is_fabricated():
    observation = build(failed(FailureClass.TRANSPORT), failed(FailureClass.TRANSPORT))
    document = observation.to_document()

    assert document["energy"]["rawDailyTotal"] is None
    assert document["energy"]["intervalValue"] is None
    assert document["state"] is None
    assert document["raw"] == {"energy": None, "state": None}
    assert observation.completeness == 0
    # Neither source succeeded, so this is not a *partial* observation.
    assert "PARTIAL_OBSERVATION" not in document["quality"]["flags"]


def test_state_is_never_carried_forward_from_a_previous_observation():
    first = build(energy_ok(), state_ok())
    second = build(energy_ok("2.200"), failed(FailureClass.TRANSPORT))

    assert first.state is not None
    assert second.state is None


def test_completeness_ranks_energy_above_state():
    both = build(energy_ok(), state_ok()).completeness
    energy_only = build(energy_ok(), failed(FailureClass.TRANSPORT)).completeness
    state_only = build(failed(FailureClass.TRANSPORT), state_ok()).completeness
    neither = build(failed(FailureClass.TRANSPORT), failed(FailureClass.TRANSPORT)).completeness

    assert both > energy_only > state_only > neither


# --- State preservation ------------------------------------------------------


def test_every_exposed_property_is_preserved_including_the_unit():
    normalized = normalize_state(STATE_PAYLOAD)

    assert normalized["airConJobMode"]["currentJobMode"] == "COOL"
    assert normalized["temperature"]["currentTemperature"] == 27
    assert normalized["temperature"]["targetTemperature"] == 24
    assert normalized["temperature"]["unit"] == "C"
    assert normalized["airFlow"]["windStrength"] == "MID"
    assert normalized["powerSave"]["powerSaveEnabled"] is False
    assert normalized["filterInfo"] == {"usedTime": 120, "filterLifetime": 1000}


def test_absent_properties_are_omitted_rather_than_defaulted():
    normalized = normalize_state(STATE_PAYLOAD)

    assert "airCleanOperationMode" not in normalized["operation"]
    assert "twoSetEnabled" not in normalized["temperature"]
    assert "emptyGroup" not in normalized
    # Nothing the device never reported has appeared.
    assert set(normalized) <= set(STATE_PAYLOAD)


def test_a_false_value_is_kept_and_not_mistaken_for_absence():
    normalized = normalize_state({"powerSave": {"powerSaveEnabled": False, "level": 0}})

    assert normalized["powerSave"]["powerSaveEnabled"] is False
    assert normalized["powerSave"]["level"] == 0


def test_an_unexpected_state_shape_normalizes_to_nothing_rather_than_a_guess():
    assert normalize_state(None) is None
    assert normalize_state("POWER_ON") is None
    assert normalize_state({}) is None


def test_principal_fields_are_pulled_out_for_display_only():
    fields = principal_fields(STATE_PAYLOAD)

    assert fields["airConOperationMode"] == "POWER_ON"
    assert fields["currentJobMode"] == "COOL"
    assert fields["targetTemperature"] == 24
    assert fields["unit"] == "C"


# --- Document shape ----------------------------------------------------------


def test_the_document_keeps_exact_decimals_and_a_queryable_mirror():
    observation = build(energy_ok(), state_ok())
    energy = observation.to_document()["energy"]

    assert energy["rawDailyTotal"] == "2.150"
    assert energy["rawDailyTotalNumber"] == 2.15
    assert energy["intervalValue"] == "0.050"
    assert energy["intervalValueNumber"] == 0.05
    assert energy["previous"]["rawDailyTotal"] == "2.100"


def test_the_document_is_json_serializable_apart_from_native_timestamps():
    document = build(energy_ok(), state_ok()).to_document()

    encoded = json.dumps(document, default=str)
    assert "2.150" in encoded
    assert "Asia/Manila" in encoded


def test_the_document_carries_both_instants_and_the_local_day():
    document = build(energy_ok(), state_ok()).to_document()

    assert document["scheduledAt"] == at(12, 5)
    assert document["observedAt"] == at(12, 5)
    assert document["localDate"] == "2026-08-20"
    assert document["timezone"] == "Asia/Manila"


def test_source_outcome_defaults_are_explicit():
    outcome = SourceOutcome(ok=False)
    assert outcome.to_document() == {
        "ok": False,
        "failureClass": None,
        "errorCode": None,
        "errorName": None,
    }


def test_an_anomalous_decrease_retains_both_raw_values():
    observation = build(
        energy_ok("3.200"),
        state_ok(),
        previous=PreviousReading("prev", at(12, 0), at(12, 0).date(), Decimal("3.500")),
    )
    energy = observation.to_document()["energy"]

    assert energy["intervalValue"] is None
    assert energy["rawDailyTotal"] == "3.200"
    assert energy["previous"]["rawDailyTotal"] == "3.500"
    assert observation.quality.interval_status is IntervalStatus.ANOMALOUS_DECREASE


def test_an_earlier_observation_is_never_rewritten_by_a_later_revision():
    earlier = build(energy_ok("3.500"), state_ok())
    later = build(
        energy_ok("3.200"),
        state_ok(),
        previous=PreviousReading("prev", at(12, 0), at(12, 0).date(), Decimal("3.500")),
    )

    # The revision is a new record carrying the classification; the earlier
    # record's stored value is untouched.
    assert earlier.raw_daily_total == Decimal("3.500")
    assert later.quality.interval_status is IntervalStatus.ANOMALOUS_DECREASE
