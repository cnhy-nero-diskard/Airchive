"""The same persistence rules, against a real Firestore.

The in-memory double is fast and covers the logic, but it is a model of
Firestore written by the same hand as the code it tests. These run against the
actual client library and the Firestore emulator, so cursor semantics,
transaction retries, dotted-path updates, and merge behavior are checked against
the real thing.

Skipped unless `FIRESTORE_EMULATOR_HOST` is set:

    firebase emulators:exec --only firestore --project demo-airchive \\
        ".venv/Scripts/python.exe -m pytest tests/test_store_emulator.py"
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from airchive.observation.build import RequestResult, build_observation
from airchive.observation.model import PreviousReading, QualityFlag
from airchive.storage.ids import floor_to_slot, sample_id_for
from airchive.storage.store import TelemetryStore
from airchive.thinq.failures import FailureClass, ThinqFailure

pytestmark = pytest.mark.skipif(
    not os.environ.get("FIRESTORE_EMULATOR_HOST"),
    reason="needs the Firestore emulator (FIRESTORE_EMULATOR_HOST)",
)

MANILA = ZoneInfo("Asia/Manila")
STATE_PAYLOAD = {"operation": {"airConOperationMode": "POWER_ON"}, "temperature": {"unit": "C"}}
ENERGY_PAYLOAD = {"unit": "kWh", "energyData": [{"date": "20260820", "value": "2.150"}]}


@pytest.fixture
def store():
    from google.cloud import firestore

    client = firestore.Client(project=os.environ.get("GCLOUD_PROJECT", "demo-airchive"))
    # A fresh device per test keeps runs independent without any cleanup step.
    return TelemetryStore(client, f"emulator-{uuid.uuid4().hex[:10]}")


def manila(hour: int, minute: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=MANILA)


def observation(*, at, value="2.150", state_ok=True, previous=None, **kwargs):
    energy = (
        RequestResult.success(ENERGY_PAYLOAD, value=Decimal(value), unit="kWh")
        if value is not None
        else RequestResult.failed(ThinqFailure(FailureClass.TRANSPORT))
    )
    state = (
        RequestResult.success(STATE_PAYLOAD)
        if state_ok
        else RequestResult.failed(ThinqFailure(FailureClass.TRANSPORT))
    )
    return build_observation(
        sample_id=sample_id_for(at, 300),
        device_id=kwargs.pop("device_id"),
        scheduled_at=floor_to_slot(at, 300),
        observed_at=at,
        local_date=at.date(),
        timezone_name="Asia/Manila",
        energy_property="energyConsumption",
        energy=energy,
        state=state,
        previous=previous,
        has_prior_observation=previous is not None,
        collector_version="0.1.0",
        **kwargs,
    )


def test_documents_land_at_the_expected_paths(store):
    now = manila(17, 15)
    store.write_observation(observation(at=now, device_id=store.device_id), now)
    store.put_daily_total("2026-08-19", Decimal("8.751"), unit="kWh", raw={}, fetched_at=now)
    store.put_metadata(
        version="v1",
        profile={"property": {"operation": {}}},
        energy_profile={"result": {"property": ["energyConsumption"]}},
        supported_energy_properties=["energyConsumption"],
        retrieved_at=now,
    )
    store.record_attempt(now, "0.1.0")

    stored = store.get_observation("20260820T091500Z")
    assert stored is not None
    assert stored["energy"]["rawDailyTotal"] == "2.150"
    assert stored["raw"]["energy"] == ENERGY_PAYLOAD
    # Firestore returns its own UTC datetimes; the instant must survive intact.
    assert stored["observedAt"] == now.astimezone(UTC)
    assert store.get_daily_total("2026-08-19") == Decimal("8.751")
    assert store.get_current_metadata()["version"] == "v1"
    assert store.get_health()["lastAttemptAt"] == now.astimezone(UTC)


def test_precedence_holds_against_the_real_transaction(store):
    now = manila(17, 15)
    device_id = store.device_id

    created = store.write_observation(
        observation(at=now, state_ok=False, device_id=device_id), now
    )
    upgraded = store.write_observation(observation(at=now, device_id=device_id), now)
    unchanged = store.write_observation(observation(at=now, device_id=device_id), now)
    refused = store.write_observation(
        observation(at=now, value=None, state_ok=False, device_id=device_id), now
    )

    assert [created.action, upgraded.action, unchanged.action, refused.action] == [
        "created",
        "upgraded",
        "unchanged",
        "refused",
    ]
    assert store.get_observation("20260820T091500Z")["completeness"] == 3
    assert len(store.latest_observations(limit=10)) == 1


def test_the_descending_id_cursor_finds_the_previous_reading(store):
    device_id = store.device_id
    for minutes, value in ((0, "2.100"), (5, None), (10, "2.200")):
        at = manila(17, 0) + timedelta(minutes=minutes)
        store.write_observation(observation(at=at, value=value, device_id=device_id), at)

    # The cursor excludes its own slot -- a cycle looks for what came *before* it.
    reading, any_prior = store.find_previous_reading(sample_id_for(manila(17, 15), 300))

    assert any_prior is True
    assert reading is not None
    assert reading.value == Decimal("2.200")
    assert reading.sample_id == sample_id_for(manila(17, 10), 300)

    # And the unusable most-recent record is skipped rather than used.
    earlier, _ = store.find_previous_reading(sample_id_for(manila(17, 8), 300))
    assert earlier is not None and earlier.value == Decimal("2.100")


def test_a_range_query_over_observed_at_needs_no_composite_index(store):
    device_id = store.device_id
    for minutes in range(0, 30, 5):
        at = manila(23, 45) + timedelta(minutes=minutes)
        store.write_observation(observation(at=at, device_id=device_id), at)

    found = store.observations_in_range(manila(23, 50), manila(0, 5, day=21))

    assert [d["observedAt"] for d in found] == [
        manila(0, 0, day=21).astimezone(UTC),
        manila(23, 55).astimezone(UTC),
        manila(23, 50).astimezone(UTC),
    ]


def test_reconciliation_patches_derived_fields_only(store):
    at = manila(0, 5, day=21)
    previous = PreviousReading(
        "20260820T155500Z", manila(23, 55), manila(23, 55).date(), Decimal("8.732")
    )
    store.write_observation(
        observation(at=at, value="0.021", previous=previous, device_id=store.device_id), at
    )
    sample_id = sample_id_for(at, 300)
    before = store.get_observation(sample_id)

    applied = store.apply_reconciliation(
        sample_id=sample_id,
        interval_value=Decimal("0.040"),
        final_previous_day_total=Decimal("8.751"),
        now=at + timedelta(hours=1),
    )

    after = store.get_observation(sample_id)
    assert applied is True
    assert after["energy"]["intervalValue"] == "0.040"
    assert after["quality"]["intervalStatus"] == "DAY_ROLLOVER_RESOLVED"
    assert str(QualityFlag.RECONCILED) in after["quality"]["flags"]
    assert after["raw"] == before["raw"]
    assert after["energy"]["rawDailyTotal"] == before["energy"]["rawDailyTotal"]
    assert after["state"] == before["state"]


def test_the_lease_is_exclusive(store):
    now = manila(17, 15)

    assert store.acquire_lease(now=now, holder="cycle-a", seconds=240) is True
    assert store.acquire_lease(now=now + timedelta(seconds=30), holder="cycle-b") is False
    assert store.acquire_lease(now=now + timedelta(seconds=300), holder="cycle-b") is True


def test_the_reconciliation_queue_can_lose_an_entry(store):
    now = manila(0, 5, day=21)
    store.enqueue_reconciliation(
        sample_id="20260820T160500Z", previous_local_date="2026-08-20", now=now
    )
    store.enqueue_reconciliation(
        sample_id="20260820T161000Z", previous_local_date="2026-08-20", now=now
    )

    store.dequeue_reconciliation("20260820T160500Z")

    assert set(store.pending_reconciliations()) == {"20260820T161000Z"}
    assert store.expired_reconciliations(now + timedelta(hours=24)) == ["20260820T161000Z"]


def test_health_counts_failures_and_resets_on_success(store):
    now = manila(17, 15)

    store.record_failure(now=now, failure_class="TRANSPORT", message="timeout")
    assert store.get_health()["consecutiveFailures"] == 1
    store.record_failure(now=now, failure_class="TRANSPORT", message="timeout")
    assert store.get_health()["consecutiveFailures"] == 2

    store.record_success(
        now=now, sample_id="20260820T091500Z", path="p", collector_version="0.1.0"
    )
    assert store.get_health()["consecutiveFailures"] == 0
