"""Persistence rules (spec: telemetry-persistence; design D6, D7, D8, D11, D12, D14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from airchive.observation.build import RequestResult, build_observation
from airchive.observation.model import IntervalStatus, PreviousReading, QualityFlag
from airchive.storage.ids import floor_to_slot, sample_id_for, slot_of_sample_id
from airchive.storage.store import TelemetryStore
from airchive.thinq.failures import FailureClass, ThinqFailure
from tests.fakes import FakeFirestoreClient

MANILA = ZoneInfo("Asia/Manila")
DEVICE = "ac-device-1"

STATE_PAYLOAD = {"operation": {"airConOperationMode": "POWER_ON"}}
ENERGY_PAYLOAD = {"unit": "kWh", "energyData": [{"date": "20260820", "value": "2.150"}]}


@pytest.fixture
def store():
    return TelemetryStore(FakeFirestoreClient(), DEVICE)


def manila(hour: int, minute: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=MANILA)


def observation(
    *,
    at: datetime,
    value: str | None = "2.150",
    state_ok: bool = True,
    previous: PreviousReading | None = None,
    **kwargs,
):
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
        device_id=DEVICE,
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


# --- Sample identifiers (design D6) -----------------------------------------


def test_the_manila_slot_stamps_as_utc():
    assert sample_id_for(manila(17, 15), 300) == "20260820T091500Z"


def test_a_late_cycle_still_belongs_to_its_slot():
    assert sample_id_for(manila(17, 15) + timedelta(seconds=200), 300) == "20260820T091500Z"
    assert sample_id_for(manila(17, 19, 20) + timedelta(seconds=59), 300) == "20260820T091500Z"


def test_identifiers_sort_lexicographically_in_chronological_order():
    ids = [sample_id_for(manila(0, 0) + timedelta(minutes=5 * i), 300) for i in range(300)]

    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_an_identifier_round_trips_to_its_slot():
    slot = floor_to_slot(manila(17, 17), 300)
    assert slot_of_sample_id(sample_id_for(manila(17, 17), 300)) == slot
    assert slot == datetime(2026, 8, 20, 9, 15, tzinfo=UTC)


def test_a_naive_instant_is_refused():
    with pytest.raises(ValueError):
        sample_id_for(datetime(2026, 8, 20, 17, 15), 300)


# --- Layout (design D8) ------------------------------------------------------


def test_documents_land_at_the_expected_paths(store):
    now = manila(17, 15)
    store.write_observation(observation(at=now), now)
    store.put_daily_total(
        "2026-08-19", Decimal("8.751"), unit="kWh", raw={}, fetched_at=now
    )
    store.put_metadata(
        version="v1",
        profile={"p": 1},
        energy_profile={"result": {"property": ["energyConsumption"]}},
        supported_energy_properties=["energyConsumption"],
        retrieved_at=now,
    )
    store.record_attempt(now, "0.1.0")
    store.enqueue_reconciliation(
        sample_id="20260820T091500Z", previous_local_date="2026-08-19", now=now
    )

    paths = set(store._client.documents)
    assert "devices/ac-device-1/telemetry/20260820T091500Z" in paths
    assert "devices/ac-device-1/dailyTotals/2026-08-19" in paths
    assert "devices/ac-device-1/metadata/v1" in paths
    assert "devices/ac-device-1/metadata/current" in paths
    assert "devices/ac-device-1/runtime/collector" in paths
    assert "devices/ac-device-1/runtime/reconciliation" in paths


# --- Idempotency and precedence (design D7) ---------------------------------


def test_repeating_a_slot_creates_exactly_one_document(store):
    now = manila(17, 15)

    first = store.write_observation(observation(at=now), now)
    second = store.write_observation(observation(at=now), now + timedelta(seconds=30))

    assert first.action == "created"
    assert second.action == "unchanged"
    telemetry = [p for p in store._client.documents if "/telemetry/" in p]
    assert len(telemetry) == 1


def test_a_more_complete_retry_upgrades_the_record(store):
    now = manila(17, 15)
    store.write_observation(observation(at=now, state_ok=False), now)

    outcome = store.write_observation(observation(at=now, state_ok=True), now)

    assert outcome.action == "upgraded"
    stored = store.get_observation("20260820T091500Z")
    assert stored["state"] is not None
    assert stored["completeness"] == 3


def test_an_equally_complete_retry_is_a_no_op(store):
    now = manila(17, 15)
    store.write_observation(observation(at=now), now)
    first_persisted = store.get_observation("20260820T091500Z")["persistedAt"]

    outcome = store.write_observation(observation(at=now + timedelta(minutes=1)), now)

    assert outcome.action == "unchanged"
    # observedAt does not churn on a duplicate.
    assert store.get_observation("20260820T091500Z")["persistedAt"] == first_persisted
    assert store.get_observation("20260820T091500Z")["observedAt"] == manila(17, 15)


def test_a_less_complete_late_write_is_refused(store):
    now = manila(17, 15)
    store.write_observation(observation(at=now), now)

    outcome = store.write_observation(observation(at=now, value=None, state_ok=False), now)

    assert outcome.action == "refused"
    assert outcome.existing_completeness == 3
    assert outcome.new_completeness == 0
    stored = store.get_observation("20260820T091500Z")
    assert stored["energy"]["rawDailyTotal"] == "2.150"


def test_a_concurrent_writer_forces_the_transaction_to_re_run(store):
    now = manila(17, 15)
    client = store._client

    def concurrent_write(fake):
        # A better record lands between this transaction's read and its commit.
        store_two = TelemetryStore(fake, DEVICE)
        fake.before_commit = None
        store_two.write_observation(observation(at=now), now)

    client.before_commit = concurrent_write
    outcome = store.write_observation(observation(at=now, state_ok=False), now)

    assert client.transaction_attempts >= 2
    # One document, consistent with precedence: the complete record survives.
    assert outcome.action in ("refused", "unchanged")
    assert store.get_observation("20260820T091500Z")["completeness"] == 3


# --- Timestamps (spec: queryable native timestamps) --------------------------


def test_all_three_instants_and_the_local_day_are_stored(store):
    now = manila(17, 15)
    store.write_observation(observation(at=now), now)

    stored = store.get_observation("20260820T091500Z")
    assert stored["scheduledAt"] == datetime(2026, 8, 20, 9, 15, tzinfo=UTC)
    assert stored["observedAt"] == now
    assert stored["persistedAt"] == now
    assert stored["localDate"] == "2026-08-20"
    assert stored["timezone"] == "Asia/Manila"


def test_a_range_query_over_observed_at_and_a_grouping_by_local_day_both_work(store):
    for minutes in range(0, 30, 5):
        at = manila(23, 45) + timedelta(minutes=minutes)
        store.write_observation(observation(at=at), at)

    found = store.observations_in_range(manila(23, 50), manila(0, 5, day=21))
    assert [d["observedAt"] for d in found] == [
        manila(0, 0, day=21),
        manila(23, 55),
        manila(23, 50),
    ]

    by_day: dict[str, int] = {}
    for document in store.latest_observations(limit=50):
        by_day[document["localDate"]] = by_day.get(document["localDate"], 0) + 1
    assert by_day == {"2026-08-20": 3, "2026-08-21": 3}


# --- Raw payloads ------------------------------------------------------------


def test_raw_payloads_are_retained_and_carry_no_credentials(store):
    now = manila(17, 15)
    store.write_observation(observation(at=now), now)

    stored = store.get_observation("20260820T091500Z")
    assert stored["raw"]["energy"] == ENERGY_PAYLOAD
    assert stored["raw"]["state"] == STATE_PAYLOAD

    encoded = repr(stored)
    for forbidden in ("Authorization", "Bearer", "x-api-key", "access_token"):
        assert forbidden not in encoded


# --- Previous-reading lookup (design D12) -----------------------------------


def test_the_lookup_finds_the_most_recent_usable_baseline(store):
    for minutes, value in ((0, "2.100"), (5, "2.150")):
        at = manila(17, 0) + timedelta(minutes=minutes)
        store.write_observation(observation(at=at, value=value), at)

    at = manila(17, 10)
    reading, any_prior = store.find_previous_reading(sample_id_for(at, 300))

    assert any_prior is True
    assert reading is not None
    assert reading.value == Decimal("2.150")
    assert reading.local_date.isoformat() == "2026-08-20"


def test_the_lookup_skips_an_unusable_most_recent_observation(store):
    at = manila(17, 0)
    store.write_observation(observation(at=at, value="2.100"), at)
    later = manila(17, 5)
    store.write_observation(observation(at=later, value=None), later)

    reading, any_prior = store.find_previous_reading(sample_id_for(manila(17, 10), 300))

    assert any_prior is True
    assert reading is not None and reading.value == Decimal("2.100")


def test_the_lookup_is_bounded_and_reports_that_prior_records_existed(store):
    for minutes in range(0, 100, 5):
        at = manila(15, 0) + timedelta(minutes=minutes)
        store.write_observation(observation(at=at, value=None), at)

    reading, any_prior = store.find_previous_reading(
        sample_id_for(manila(17, 0), 300), limit=12
    )

    assert reading is None
    assert any_prior is True


def test_an_empty_series_reports_no_prior_observation(store):
    reading, any_prior = store.find_previous_reading(sample_id_for(manila(17, 0), 300))

    assert reading is None
    assert any_prior is False


# --- Metadata (spec: static metadata stored separately) ---------------------


def test_an_observation_references_metadata_without_embedding_it(store):
    now = manila(17, 15)
    store.put_metadata(
        version="v1",
        profile={"property": {"operation": {}}},
        energy_profile={"result": {"property": ["energyConsumption"]}},
        supported_energy_properties=["energyConsumption"],
        retrieved_at=now,
    )
    store.write_observation(observation(at=now, metadata_version="v1"), now)

    stored = store.get_observation("20260820T091500Z")
    assert stored["metadataVersion"] == "v1"
    assert "profile" not in stored
    assert store.get_current_metadata()["version"] == "v1"
    assert store._client.documents["devices/ac-device-1/metadata/v1"]["profile"] == {
        "property": {"operation": {}}
    }


def test_a_changed_profile_keeps_the_earlier_version_readable(store):
    now = manila(17, 15)
    store.put_metadata(
        version="v1",
        profile={"a": 1},
        energy_profile={},
        supported_energy_properties=["energyConsumption"],
        retrieved_at=now,
    )
    store.put_metadata(
        version="v2",
        profile={"a": 2},
        energy_profile={},
        supported_energy_properties=["energyConsumption", "powerConsumption"],
        retrieved_at=now + timedelta(days=1),
    )

    assert store.get_current_metadata()["version"] == "v2"
    assert store._client.documents["devices/ac-device-1/metadata/v1"]["profile"] == {"a": 1}


# --- Daily totals cache (design D8) -----------------------------------------


def test_a_finalized_total_is_fetched_once_and_reused(store):
    now = manila(0, 5, day=21)
    assert store.get_daily_total("2026-08-20") is None

    store.put_daily_total("2026-08-20", Decimal("8.751"), unit="kWh", raw={}, fetched_at=now)

    writes_after_cache = len(store._client.writes)
    for _ in range(5):
        assert store.get_daily_total("2026-08-20") == Decimal("8.751")
    assert len(store._client.writes) == writes_after_cache


# --- Health (spec: collector health is separate from history) ---------------


def test_health_is_overwritten_in_place_and_never_appended_to_the_series(store):
    now = manila(17, 15)

    store.record_attempt(now, "0.1.0")
    store.record_failure(
        now=now, failure_class="TRANSPORT", message="timeout", collector_version="0.1.0"
    )
    store.record_failure(
        now=now + timedelta(minutes=5),
        failure_class="TRANSPORT",
        message="timeout",
        collector_version="0.1.0",
    )

    health = store.get_health()
    assert health["consecutiveFailures"] == 2
    assert health["lastErrorClass"] == "TRANSPORT"
    assert len([p for p in store._client.documents if "/runtime/collector" in p]) == 1
    assert not [p for p in store._client.documents if "/telemetry/" in p]


def test_the_failure_counter_resets_on_success(store):
    now = manila(17, 15)
    store.record_failure(now=now, failure_class="TRANSIENT", message="boom")
    store.record_failure(now=now, failure_class="TRANSIENT", message="boom")

    store.record_success(
        now=now + timedelta(minutes=5),
        sample_id="20260820T091500Z",
        path="devices/ac-device-1/telemetry/20260820T091500Z",
        collector_version="0.1.0",
    )

    health = store.get_health()
    assert health["consecutiveFailures"] == 0
    assert health["lastSampleId"] == "20260820T091500Z"
    # The failure history is still visible, just not the count.
    assert health["lastErrorClass"] == "TRANSIENT"


# --- Lease (design D14) ------------------------------------------------------


def test_a_second_concurrent_cycle_cannot_take_the_lease(store):
    now = manila(17, 15)

    assert store.acquire_lease(now=now, holder="cycle-a", seconds=240) is True
    assert store.acquire_lease(now=now + timedelta(seconds=30), holder="cycle-b") is False


def test_the_lease_expires_so_a_crashed_cycle_does_not_block_forever(store):
    now = manila(17, 15)
    store.acquire_lease(now=now, holder="cycle-a", seconds=240)

    assert store.acquire_lease(now=now + timedelta(seconds=241), holder="cycle-b") is True


def test_releasing_the_lease_frees_it_for_the_next_cycle(store):
    now = manila(17, 15)
    store.acquire_lease(now=now, holder="cycle-a", seconds=240)
    store.release_lease("cycle-a")

    assert store.acquire_lease(now=now + timedelta(seconds=10), holder="cycle-b") is True


def test_a_cycle_cannot_release_a_lease_it_does_not_hold(store):
    now = manila(17, 15)
    store.acquire_lease(now=now, holder="cycle-a", seconds=240)
    store.release_lease("cycle-b")

    assert store.acquire_lease(now=now + timedelta(seconds=10), holder="cycle-c") is False


# --- Reconciliation (design D11) --------------------------------------------


def test_an_unresolved_rollover_is_filled_in_when_the_finalized_total_arrives(store):
    at = manila(0, 5, day=21)
    previous = PreviousReading(
        "20260820T155500Z", manila(23, 55), manila(23, 55).date(), Decimal("8.732")
    )
    store.write_observation(
        observation(at=at, value="0.021", previous=previous), at
    )
    sample_id = sample_id_for(at, 300)
    store.enqueue_reconciliation(sample_id=sample_id, previous_local_date="2026-08-20", now=at)

    before = store.get_observation(sample_id)
    assert before["quality"]["intervalStatus"] == "DAY_ROLLOVER_UNRESOLVED"
    assert sample_id in store.pending_reconciliations()

    applied = store.apply_reconciliation(
        sample_id=sample_id,
        interval_value=Decimal("0.040"),
        final_previous_day_total=Decimal("8.751"),
        now=at + timedelta(hours=1),
    )
    store.dequeue_reconciliation(sample_id)

    after = store.get_observation(sample_id)
    assert applied is True
    assert after["quality"]["intervalStatus"] == "DAY_ROLLOVER_RESOLVED"
    assert str(QualityFlag.RECONCILED) in after["quality"]["flags"]
    assert after["energy"]["intervalValue"] == "0.040"
    assert after["energy"]["finalPreviousDayTotal"] == "8.751"
    assert after["reconciledAt"] == at + timedelta(hours=1)
    assert store.pending_reconciliations() == {}


def test_reconciliation_never_alters_a_stored_raw_value(store):
    at = manila(0, 5, day=21)
    previous = PreviousReading(
        "20260820T155500Z", manila(23, 55), manila(23, 55).date(), Decimal("8.732")
    )
    store.write_observation(observation(at=at, value="0.021", previous=previous), at)
    sample_id = sample_id_for(at, 300)
    before = store.get_observation(sample_id)

    store.apply_reconciliation(
        sample_id=sample_id,
        interval_value=Decimal("0.040"),
        final_previous_day_total=Decimal("8.751"),
        now=at + timedelta(hours=1),
    )

    after = store.get_observation(sample_id)
    assert after["raw"] == before["raw"]
    assert after["energy"]["rawDailyTotal"] == before["energy"]["rawDailyTotal"]
    assert after["energy"]["previous"] == before["energy"]["previous"]
    assert after["observedAt"] == before["observedAt"]


def test_reconciliation_is_abandoned_after_the_window(store):
    at = manila(0, 5, day=21)
    sample_id = "20260820T160500Z"
    store.enqueue_reconciliation(sample_id=sample_id, previous_local_date="2026-08-20", now=at)

    assert store.expired_reconciliations(at + timedelta(hours=23)) == []
    assert store.expired_reconciliations(at + timedelta(hours=24)) == [sample_id]

    store.dequeue_reconciliation(sample_id)
    assert store.pending_reconciliations() == {}


def test_reconciling_a_missing_observation_reports_failure_rather_than_creating_one(store):
    applied = store.apply_reconciliation(
        sample_id="20260820T160500Z",
        interval_value=Decimal("0.040"),
        final_previous_day_total=Decimal("8.751"),
        now=manila(1, 0, day=21),
    )

    assert applied is False
    assert not [p for p in store._client.documents if "/telemetry/" in p]


# --- Anomaly detection -------------------------------------------------------


def test_anomalous_documents_are_recognized_by_status_or_flag():
    assert TelemetryStore.is_anomalous(
        {"quality": {"intervalStatus": str(IntervalStatus.ANOMALOUS_DECREASE), "flags": []}}
    )
    assert TelemetryStore.is_anomalous(
        {"quality": {"intervalStatus": "NORMAL", "flags": [str(QualityFlag.DEVICE_OFFLINE)]}}
    )
    assert not TelemetryStore.is_anomalous(
        {"quality": {"intervalStatus": "NORMAL", "flags": [str(QualityFlag.UNCHANGED_COUNTER)]}}
    )
