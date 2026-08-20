"""Operator inspection commands (spec: collector-operations).

Each runs against seeded storage, offline. The last test is the one that matters
most: with a sentinel token in the environment, no command may print it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from airchive.commands import views
from airchive.observation.build import RequestResult, build_observation
from airchive.observation.model import (
    IntervalStatus,
    Observation,
    PreviousReading,
    Quality,
    QualityFlag,
    SourceOutcome,
)
from airchive.storage.ids import floor_to_slot, sample_id_for
from airchive.storage.store import TelemetryStore
from airchive.thinq.client import ThinqClient
from airchive.thinq.failures import FailureClass, ThinqFailure
from tests.fakes import FakeFirestoreClient, FakeThinqApi

MANILA = ZoneInfo("Asia/Manila")
DEVICE = "ac-device-1"
SENTINEL_PAT = "SENTINEL-PAT-31d0c8a7f2b64e19"

STATE = {"operation": {"airConOperationMode": "POWER_ON"}, "temperature": {"unit": "C"}}


def utc(hour: int, minute: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def seeded(*, values=(("2.100", 0), ("2.150", 5), ("2.200", 10))) -> TelemetryStore:
    store = TelemetryStore(FakeFirestoreClient(), DEVICE)
    previous = None
    for value, minutes in values:
        at = utc(9, 0) + timedelta(minutes=minutes)
        observation = build_observation(
            sample_id=sample_id_for(at, 300),
            device_id=DEVICE,
            scheduled_at=floor_to_slot(at, 300),
            observed_at=at,
            local_date=at.astimezone(MANILA).date(),
            timezone_name="Asia/Manila",
            energy_property="energyConsumption",
            energy=RequestResult.success({"value": value}, value=Decimal(value), unit="kWh"),
            state=RequestResult.success(STATE),
            previous=previous,
            has_prior_observation=previous is not None,
            collector_version="0.1.0",
        )
        store.write_observation(observation, at)
        previous = PreviousReading(
            observation.sample_id, at, observation.local_date, Decimal(value)
        )
    return store


def seed_anomaly(
    store: TelemetryStore,
    *,
    at: datetime,
    status: IntervalStatus,
    flags: tuple[QualityFlag, ...] = (),
) -> str:
    quality = Quality(interval_status=status)
    for flag in flags:
        quality.add(flag)
    observation = Observation(
        sample_id=sample_id_for(at, 300),
        device_id=DEVICE,
        scheduled_at=floor_to_slot(at, 300),
        observed_at=at,
        local_date=at.astimezone(MANILA).date(),
        timezone_name="Asia/Manila",
        quality=quality,
        energy_source=SourceOutcome(ok=True),
        state_source=SourceOutcome(ok=True),
        energy_property="energyConsumption",
        raw_daily_total=Decimal("3.200"),
        unit="kWh",
        collector_version="0.1.0",
    )
    store.write_observation(observation, at)
    return observation.sample_id


# --- latest ------------------------------------------------------------------


def test_latest_lists_observations_newest_first(capsys):
    exit_code = views.latest(limit=10, store=seeded())
    out = capsys.readouterr().out

    assert exit_code == 0
    rows = [line for line in out.splitlines() if line.startswith("20260820T")]
    assert [row.split()[0] for row in rows] == [
        "20260820T091000Z",
        "20260820T090500Z",
        "20260820T090000Z",
    ]

    newest = rows[0]
    assert "2.200" in newest  # raw cumulative value
    assert "0.050" in newest  # derived interval
    assert "300s" in newest  # duration from actual observation times
    assert "NORMAL" in newest  # interval status
    assert "energy=ok state=ok" in newest  # source outcomes
    assert "devices/ac-device-1/telemetry/20260820T091000Z" in out


def test_latest_honors_the_limit(capsys):
    views.latest(limit=2, store=seeded())
    rows = [line for line in capsys.readouterr().out.splitlines() if line.startswith("20260820T")]
    assert len(rows) == 2


def test_latest_on_an_empty_series_says_so(capsys):
    exit_code = views.latest(store=TelemetryStore(FakeFirestoreClient(), DEVICE))

    assert exit_code == 0
    assert "No observations stored" in capsys.readouterr().out


# --- health ------------------------------------------------------------------


def test_health_reports_a_seeded_failure_state(capsys):
    store = seeded()
    store.record_failure(now=utc(9, 15), failure_class="AUTH_FATAL", message="invalid token")
    store.record_failure(now=utc(9, 20), failure_class="AUTH_FATAL", message="invalid token")
    store.record_attempt(utc(9, 20), "0.1.0")

    exit_code = views.health(store=store)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "consecutiveFailures: 2" in out
    assert "lastErrorClass:      AUTH_FATAL" in out
    assert "invalid token" in out
    assert "collectorVersion:    0.1.0" in out


def test_health_reports_the_last_successful_cycle_and_its_path(capsys):
    store = seeded()
    store.record_success(
        now=utc(9, 10),
        sample_id="20260820T091000Z",
        path="devices/ac-device-1/telemetry/20260820T091000Z",
        collector_version="0.1.0",
    )

    views.health(store=store)
    out = capsys.readouterr().out

    assert "lastSampleId:        20260820T091000Z" in out
    assert "lastSamplePath:      devices/ac-device-1/telemetry/20260820T091000Z" in out


def test_health_shows_pending_reconciliations(capsys):
    store = seeded()
    store.enqueue_reconciliation(
        sample_id="20260820T160500Z", previous_local_date="2026-08-20", now=utc(16, 5)
    )

    views.health(store=store)
    out = capsys.readouterr().out

    assert "pendingReconciliations: 1" in out
    assert "20260820T160500Z awaiting 2026-08-20" in out


def test_health_before_the_first_run(capsys):
    views.health(store=TelemetryStore(FakeFirestoreClient(), DEVICE))
    assert "has not run" in capsys.readouterr().out


# --- anomalies ---------------------------------------------------------------


def test_anomalies_surfaces_seeded_problems_and_skips_healthy_records(capsys):
    store = seeded()
    decrease = seed_anomaly(store, at=utc(9, 15), status=IntervalStatus.ANOMALOUS_DECREASE)
    rollover = seed_anomaly(
        store, at=utc(9, 20), status=IntervalStatus.DAY_ROLLOVER_UNRESOLVED
    )

    exit_code = views.anomalies(
        since=utc(8, 0).isoformat(), until=utc(10, 0).isoformat(), store=store
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert decrease in out
    assert rollover in out
    assert "ANOMALOUS_DECREASE" in out
    assert "DAY_ROLLOVER_UNRESOLVED" in out
    # The three healthy observations are not listed.
    assert "20260820T090500Z" not in out
    assert "scanned 5, flagged 2" in out


def test_anomalies_finds_a_flagged_but_otherwise_normal_observation(capsys):
    store = seeded()
    offline = seed_anomaly(
        store,
        at=utc(9, 15),
        status=IntervalStatus.NORMAL,
        flags=(QualityFlag.DEVICE_OFFLINE, QualityFlag.RATE_LIMITED),
    )

    views.anomalies(since=utc(8, 0).isoformat(), until=utc(10, 0).isoformat(), store=store)
    out = capsys.readouterr().out

    assert offline in out
    assert "DEVICE_OFFLINE" in out
    assert "RATE_LIMITED" in out


def test_anomalies_defaults_to_the_last_day(capsys):
    store = seeded()
    seed_anomaly(store, at=utc(9, 15), status=IntervalStatus.ANOMALOUS_DECREASE)

    views.anomalies(store=store, now=utc(12, 0))
    assert "ANOMALOUS_DECREASE" in capsys.readouterr().out

    views.anomalies(store=store, now=utc(12, 0, day=25))
    assert "No anomalous observations" in capsys.readouterr().out


def test_anomalies_rejects_an_unreadable_range(capsys):
    assert views.anomalies(since="not-a-date", store=seeded()) == 2
    assert "Could not read the time range" in capsys.readouterr().err


def test_a_clean_range_reports_nothing_found(capsys):
    views.anomalies(since=utc(8, 0).isoformat(), until=utc(10, 0).isoformat(), store=seeded())
    assert "No anomalous observations" in capsys.readouterr().out


# --- compare -----------------------------------------------------------------


@pytest.fixture
def compare_env(monkeypatch):
    for name, value in {
        "LG_THINQ_PAT": SENTINEL_PAT,
        "LG_COUNTRY_CODE": "PH",
        "LG_CLIENT_ID": "1f0b3a6e-9c7d-4b2a-8e51-0c3d5f7a9b11",
        "LG_DEVICE_ID": DEVICE,
        "LG_ENERGY_PROPERTY": "energyConsumption",
        "FIREBASE_PROJECT_ID": "lg-ac-telemetry",
        "LG_DAY_TIMEZONE": "Asia/Manila",
    }.items():
        monkeypatch.setenv(name, value)


def test_compare_diffs_stored_against_live_and_writes_nothing(compare_env, capsys):
    store = seeded()
    writes_before = list(store._client.writes)
    api = FakeThinqApi(
        energy_usage={"unit": "kWh", "value": "2.400"},
        device_status={
            "operation": {"airConOperationMode": "POWER_OFF"},
            "temperature": {"unit": "C"},
        },
    )

    exit_code = views.compare(store=store, client=ThinqClient(api))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "2.200" in out  # stored
    assert "2.400" in out  # live
    assert "POWER_ON" in out and "POWER_OFF" in out
    assert "*airConOperationMode" in out  # marked as differing
    assert store._client.writes == writes_before  # nothing written


def test_compare_reports_a_live_failure_without_writing(compare_env, capsys):
    from tests.fakes import thinq_api_exception

    store = seeded()
    writes_before = list(store._client.writes)
    api = FakeThinqApi(
        energy_usage=thinq_api_exception("1222", "device offline", token=SENTINEL_PAT),
        device_status=thinq_api_exception("1222", "device offline", token=SENTINEL_PAT),
    )

    exit_code = views.compare(store=store, client=ThinqClient(api))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "DEVICE_OFFLINE" in out
    assert SENTINEL_PAT not in out
    assert store._client.writes == writes_before


# --- No command prints a secret ---------------------------------------------


def test_no_inspection_command_prints_the_token(compare_env, capsys):
    store = seeded()
    store.record_failure(
        now=utc(9, 15), failure_class="AUTH_FATAL", message="invalid token"
    )
    seed_anomaly(store, at=utc(9, 15), status=IntervalStatus.ANOMALOUS_DECREASE)
    api = FakeThinqApi(energy_usage={"unit": "kWh", "value": "2.400"}, device_status=STATE)

    views.latest(store=store)
    views.health(store=store)
    views.anomalies(since=utc(8, 0).isoformat(), until=utc(10, 0).isoformat(), store=store)
    views.compare(store=store, client=ThinqClient(api))

    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert SENTINEL_PAT not in stream
        assert "Bearer" not in stream
        assert "Authorization" not in stream


def test_a_failure_carrying_the_token_is_rendered_without_it(capsys):
    from airchive.redaction import clear_secrets, register_secret

    register_secret(SENTINEL_PAT)
    try:
        failure = ThinqFailure(
            FailureClass.AUTH_FATAL,
            code="1103",
            error_name="INVALID_TOKEN",
            safe_message="invalid token",
        )
        store = seeded()
        store.record_failure(
            now=utc(9, 15),
            failure_class=str(failure.failure_class),
            message=failure.safe_message,
        )

        views.health(store=store)
        assert SENTINEL_PAT not in capsys.readouterr().out
    finally:
        clear_secrets()
