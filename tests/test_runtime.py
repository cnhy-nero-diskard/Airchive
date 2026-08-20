"""Runtime behavior (spec: collector-runtime).

Everything here runs offline against the test doubles — no credentials, no
device, and no real waiting: the clock, the sleep, and the jitter roll are all
injected.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from airchive.logging_setup import CONTEXT_KEY, JsonFormatter, configure_logging
from airchive.observation.model import IntervalStatus, QualityFlag
from airchive.redaction import clear_secrets, register_secret
from airchive.runtime.cycle import CycleSettings, run_cycle
from airchive.runtime.poller import run_loop, seconds_until_next_slot
from airchive.runtime.retry import (
    AttemptLog,
    RetryPolicy,
    call_with_retries,
    rate_limit_cooldown_seconds,
)
from airchive.storage.store import TelemetryStore
from airchive.thinq.client import ThinqClient
from airchive.thinq.failures import FailureClass, ThinqFailure, ThinqRequestError
from tests.fakes import FakeFirestoreClient, FakeThinqApi, sequence, thinq_api_exception

MANILA = ZoneInfo("Asia/Manila")
DEVICE = "ac-device-1"

PROFILE = {"property": {"operation": {"airConOperationMode": {"type": "enum"}}}}
ENERGY_PROFILE = {"result": {"property": ["energyConsumption"]}}
STATE = {"operation": {"airConOperationMode": "POWER_ON"}, "temperature": {"unit": "C"}}


def usage(value: str, day: str = "20260820") -> dict:
    return {"unit": "kWh", "energyData": [{"date": day, "value": value}]}


def settings(**kwargs) -> CycleSettings:
    return CycleSettings(
        device_id=DEVICE,
        energy_property="energyConsumption",
        timezone=MANILA,
        timezone_name="Asia/Manila",
        interval_seconds=300,
        collector_version="0.1.0",
        **kwargs,
    )


def utc(hour: int, minute: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def warm_store(client: FakeFirestoreClient | None = None) -> TelemetryStore:
    """A store whose device metadata is already cached."""
    store = TelemetryStore(client or FakeFirestoreClient(), DEVICE)
    store.put_metadata(
        version="v1",
        profile=PROFILE,
        energy_profile=ENERGY_PROFILE,
        supported_energy_properties=["energyConsumption"],
        retrieved_at=utc(0),
    )
    return store


def no_wait(**kwargs):
    """Retry knobs that make every delay instantaneous and deterministic."""
    recorded: list[float] = []

    async def sleep(seconds: float) -> None:
        recorded.append(seconds)

    ticks = iter(range(0, 10_000))
    return (
        {"sleep": sleep, "roll": lambda: 0.5, "monotonic": lambda: next(ticks) * 0.001},
        recorded,
    )


# --- Per-cycle call budget ---------------------------------------------------


async def test_a_routine_cycle_issues_exactly_two_requests():
    api = FakeThinqApi(energy_usage=usage("2.150"), device_status=STATE)
    store = warm_store()

    result = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 15)
    )

    assert api.call_count("async_get_device_energy_usage") == 1
    assert api.call_count("async_get_device_status") == 1
    assert api.call_count("async_get_device_list") == 0
    assert api.call_count("async_get_device_profile") == 0
    assert api.call_count("async_get_device_energy_profile") == 0
    assert result.wrote


async def test_a_cold_start_warms_metadata_once():
    api = FakeThinqApi(
        energy_usage=usage("2.150"),
        device_status=STATE,
        device_profile=PROFILE,
        energy_profile=ENERGY_PROFILE,
    )
    store = TelemetryStore(FakeFirestoreClient(), DEVICE)

    await run_cycle(client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 15))
    await run_cycle(client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 20))

    assert api.call_count("async_get_device_profile") == 1
    assert api.call_count("async_get_device_energy_profile") == 1
    assert store.get_current_metadata()["supportedEnergyProperties"] == ["energyConsumption"]


async def test_an_unsupported_energy_property_is_fatal_not_retried():
    api = FakeThinqApi(
        energy_usage=usage("2.150"),
        device_status=STATE,
        device_profile=PROFILE,
        energy_profile={"result": {"property": ["powerConsumption"]}},
    )
    store = TelemetryStore(FakeFirestoreClient(), DEVICE)

    result = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 15)
    )

    assert result.write is None
    assert "powerConsumption" in (result.error or "")
    assert store.get_health()["lastErrorClass"] == "CONFIG_FATAL"


# --- Retries and backoff -----------------------------------------------------


async def test_transient_failures_are_retried_a_bounded_number_of_times():
    knobs, delays = no_wait()
    attempts = AttemptLog()
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise ThinqRequestError(ThinqFailure(FailureClass.TRANSIENT, code="2210"))

    with pytest.raises(ThinqRequestError):
        await call_with_retries(operation, RetryPolicy(max_attempts=3), log=attempts, **knobs)

    assert calls == 3
    assert attempts.attempts == 3
    # Two waits between three attempts, and neither of them is zero.
    assert len(delays) == 2
    assert all(delay > 0 for delay in delays)
    assert delays[1] > delays[0]


async def test_a_retry_succeeding_returns_its_value():
    knobs, _ = no_wait()
    responses = iter([ThinqRequestError(ThinqFailure(FailureClass.TRANSPORT)), "ok"])

    async def operation():
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    assert await call_with_retries(operation, RetryPolicy(), **knobs) == "ok"


async def test_retries_stop_when_the_time_budget_would_be_exceeded():
    delays: list[float] = []

    async def sleep(seconds):
        delays.append(seconds)

    clock = iter([0.0, 40.0, 80.0, 120.0, 160.0])

    async def operation():
        raise ThinqRequestError(ThinqFailure(FailureClass.TRANSIENT))

    with pytest.raises(ThinqRequestError):
        await call_with_retries(
            operation,
            RetryPolicy(max_attempts=5, total_budget_seconds=45.0),
            sleep=sleep,
            monotonic=lambda: next(clock),
            roll=lambda: 0.5,
        )

    # The budget cut it short well before the attempt limit.
    assert len(delays) <= 2


@pytest.mark.parametrize(
    "failure_class", [FailureClass.AUTH_FATAL, FailureClass.CONFIG_FATAL]
)
async def test_fatal_conditions_consume_no_retries(failure_class):
    knobs, delays = no_wait()
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise ThinqRequestError(ThinqFailure(failure_class, code="1103"))

    with pytest.raises(ThinqRequestError):
        await call_with_retries(operation, RetryPolicy(max_attempts=5), **knobs)

    assert calls == 1
    assert delays == []


async def test_a_revoked_token_is_not_retried_and_surfaces_in_health():
    api = FakeThinqApi(
        energy_usage=thinq_api_exception("1103", "invalid token"),
        device_status=thinq_api_exception("1103", "invalid token"),
    )
    store = warm_store()
    knobs, delays = no_wait()

    result = await run_cycle(
        client=ThinqClient(api),
        store=store,
        settings=settings(),
        now=utc(9, 15),
        **knobs,
    )

    assert delays == []
    assert api.call_count("async_get_device_energy_usage") == 1
    document = store.get_observation(result.sample_id)
    assert document["source"]["energy"]["failureClass"] == "AUTH_FATAL"
    assert store.get_health()["lastErrorClass"] == "AUTH_FATAL"
    assert store.get_health()["consecutiveFailures"] == 1


async def test_rate_limit_backoff_is_bounded_and_jittered():
    knobs, delays = no_wait()

    async def operation():
        raise ThinqRequestError(ThinqFailure(FailureClass.RATE_LIMITED, code="1306"))

    with pytest.raises(ThinqRequestError):
        await call_with_retries(
            operation,
            RetryPolicy(max_attempts=3, rate_limit_base_delay_seconds=5.0),
            **knobs,
        )

    assert delays == [5.0, 10.0]

    policy = RetryPolicy(jitter=0.5, rate_limit_base_delay_seconds=5.0)
    low = policy.delay_for(1, FailureClass.RATE_LIMITED, roll=0.0)
    high = policy.delay_for(1, FailureClass.RATE_LIMITED, roll=1.0)
    # Symmetric +/-50% jitter around the backoff.
    assert low == pytest.approx(2.5)
    assert high == pytest.approx(7.5)


async def test_sustained_rate_limiting_reduces_the_request_rate_across_cycles():
    api = FakeThinqApi(
        energy_usage=thinq_api_exception("1306", "quota exceeded"),
        device_status=thinq_api_exception("1306", "quota exceeded"),
    )
    store = warm_store()
    knobs, _ = no_wait()

    first = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 15), **knobs
    )
    calls_after_first = api.call_count("async_get_device_energy_usage")

    second = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 20), **knobs
    )

    # The second cycle issued nothing at all: that is the reduced rate.
    assert api.call_count("async_get_device_energy_usage") == calls_after_first
    for result in (first, second):
        document = store.get_observation(result.sample_id)
        assert str(QualityFlag.RATE_LIMITED) in document["quality"]["flags"]
    assert store.get_health()["consecutiveRateLimits"] == 2


async def test_the_cooldown_clears_once_requests_succeed():
    store = warm_store()
    knobs, _ = no_wait()
    failing = FakeThinqApi(
        energy_usage=thinq_api_exception("1306"), device_status=thinq_api_exception("1306")
    )
    await run_cycle(
        client=ThinqClient(failing), store=store, settings=settings(), now=utc(9, 15), **knobs
    )

    # Past the cooldown, a successful cycle resets the state.
    healthy = FakeThinqApi(energy_usage=usage("2.150"), device_status=STATE)
    await run_cycle(
        client=ThinqClient(healthy), store=store, settings=settings(), now=utc(11, 0), **knobs
    )

    health = store.get_health()
    assert health["consecutiveRateLimits"] == 0
    assert health["rateLimitedUntil"] is None
    assert healthy.call_count("async_get_device_energy_usage") == 1


def test_the_cooldown_grows_and_is_capped():
    assert rate_limit_cooldown_seconds(0) == 0
    first = rate_limit_cooldown_seconds(1, roll=0.5)
    second = rate_limit_cooldown_seconds(2, roll=0.5)
    assert second == 2 * first
    assert rate_limit_cooldown_seconds(20, roll=0.5) <= 3600


# --- Error containment -------------------------------------------------------


async def test_a_malformed_response_ends_the_cycle_not_the_collector():
    api = FakeThinqApi(energy_usage={"unexpected": "shape"}, device_status=STATE)
    store = warm_store()

    first = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 15)
    )

    api.energy_usage = usage("2.150")
    second = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 20)
    )

    assert store.get_observation(first.sample_id)["source"]["energy"]["failureClass"] == (
        "MALFORMED"
    )
    assert second.wrote
    assert store.get_observation(second.sample_id)["energy"]["rawDailyTotal"] == "2.150"


async def test_an_unexpected_exception_is_contained_and_recorded():
    api = FakeThinqApi(energy_usage=usage("2.150"), device_status=STATE)
    store = warm_store()

    def explode(*_args, **_kwargs):
        raise RuntimeError("firestore is having a day")

    store.find_previous_reading = explode  # type: ignore[method-assign]
    result = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 15)
    )

    assert result.error is not None
    assert "RuntimeError" in result.error
    assert store.get_health()["lastErrorClass"] == "CYCLE_ERROR"

    # And the collector carries on.
    store_two = warm_store()
    ok = await run_cycle(
        client=ThinqClient(api), store=store_two, settings=settings(), now=utc(9, 20)
    )
    assert ok.wrote


async def test_a_persistence_failure_is_recorded_and_the_next_cycle_proceeds():
    api = FakeThinqApi(energy_usage=usage("2.150"), device_status=STATE)
    store = warm_store()
    original = store.write_observation

    def fail_once(observation, now):
        store.write_observation = original  # type: ignore[method-assign]
        raise RuntimeError("write failed")

    store.write_observation = fail_once  # type: ignore[method-assign]

    failed = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 15)
    )
    recovered = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 20)
    )

    assert failed.write is None and failed.error is not None
    assert recovered.wrote


# --- Restart and stateless recovery -----------------------------------------


async def test_a_restart_reconstructs_the_previous_reading_from_storage():
    client = FakeFirestoreClient()
    store = warm_store(client)
    api = FakeThinqApi(energy_usage=usage("4.210"), device_status=STATE)

    await run_cycle(client=ThinqClient(api), store=store, settings=settings(), now=utc(4, 0))

    # A whole new process: nothing in memory, everything from Firestore.
    restarted = TelemetryStore(client, DEVICE)
    api.energy_usage = usage("4.370")
    result = await run_cycle(
        client=ThinqClient(api), store=restarted, settings=settings(), now=utc(4, 20)
    )

    document = restarted.get_observation(result.sample_id)
    assert document["energy"]["intervalValue"] == "0.160"
    assert document["energy"]["intervalSeconds"] == pytest.approx(1200)
    assert document["quality"]["intervalStatus"] == str(IntervalStatus.COARSE_INTERVAL)


async def test_a_restart_across_a_day_boundary_routes_to_rollover_handling():
    client = FakeFirestoreClient()
    store = warm_store(client)
    # 15:55 UTC is 23:55 in Manila.
    api = FakeThinqApi(energy_usage=usage("8.732"), device_status=STATE)
    await run_cycle(client=ThinqClient(api), store=store, settings=settings(), now=utc(15, 55))

    restarted = TelemetryStore(client, DEVICE)

    # 16:05 UTC is 00:05 the next Manila day. LG has not finalized yesterday:
    # asking for the 20th returns a response with no data for it.
    def by_day(_device, _prop, _period, start, _end):
        if start == "20260820":
            return {"unit": "kWh", "energyData": []}
        return {"unit": "kWh", "energyData": [{"date": "20260821", "value": "0.021"}]}

    api.energy_usage = by_day
    result = await run_cycle(
        client=ThinqClient(api), store=restarted, settings=settings(), now=utc(16, 5)
    )

    document = restarted.get_observation(result.sample_id)
    assert document["quality"]["intervalStatus"] == str(IntervalStatus.DAY_ROLLOVER_UNRESOLVED)
    assert document["energy"]["intervalValue"] is None
    assert result.sample_id in restarted.pending_reconciliations()


async def test_a_deferred_rollover_is_reconciled_when_the_total_arrives():
    client = FakeFirestoreClient()
    store = warm_store(client)
    api = FakeThinqApi(energy_usage=usage("8.732"), device_status=STATE)
    await run_cycle(client=ThinqClient(api), store=store, settings=settings(), now=utc(15, 55))

    # Rollover cycle: yesterday's finalized total is not available yet.
    def energy_responder(_device, _prop, _period, start, _end):
        if start == "20260820":
            raise thinq_api_exception("2210", "not ready")
        return {"unit": "kWh", "energyData": [{"date": "20260821", "value": "0.021"}]}

    api.energy_usage = energy_responder
    knobs, _ = no_wait()
    rollover = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(16, 5), **knobs
    )
    assert rollover.sample_id in store.pending_reconciliations()

    # A later cycle finds the finalized total and fills the gap in.
    def settled(_device, _prop, _period, start, _end):
        if start == "20260820":
            return {"unit": "kWh", "energyData": [{"date": "20260820", "value": "8.751"}]}
        return {"unit": "kWh", "energyData": [{"date": "20260821", "value": "0.030"}]}

    api.energy_usage = settled
    await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(16, 10), **knobs
    )

    document = store.get_observation(rollover.sample_id)
    assert document["energy"]["intervalValue"] == "0.040"
    assert document["quality"]["intervalStatus"] == str(IntervalStatus.DAY_ROLLOVER_RESOLVED)
    assert str(QualityFlag.RECONCILED) in document["quality"]["flags"]
    assert store.pending_reconciliations() == {}


async def test_reconciliation_is_abandoned_after_its_window():
    client = FakeFirestoreClient()
    store = warm_store(client)
    store.enqueue_reconciliation(
        sample_id="20260820T160500Z", previous_local_date="2026-08-20", now=utc(16, 5)
    )
    api = FakeThinqApi(energy_usage=usage("1.000", day="20260822"), device_status=STATE)

    await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(17, 0, day=21)
    )

    assert store.pending_reconciliations() == {}


# --- Overlap prevention ------------------------------------------------------


async def test_a_second_concurrent_cycle_exits_without_writing():
    api = FakeThinqApi(energy_usage=usage("2.150"), device_status=STATE)
    store = warm_store()
    store.acquire_lease(now=utc(9, 15), holder="other-cycle", seconds=240)

    result = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 15)
    )

    assert result.skipped == "lease"
    assert result.write is None
    assert api.calls == []
    assert not [p for p in store._client.documents if "/telemetry/" in p]


# --- Graceful shutdown -------------------------------------------------------


async def test_shutdown_mid_cycle_leaves_no_partial_observation():
    shutdown = asyncio.Event()

    async def stop_after_reading(*_args, **_kwargs):
        shutdown.set()
        return STATE

    api = FakeThinqApi(energy_usage=usage("2.150"))
    api.async_get_device_status = stop_after_reading  # type: ignore[method-assign]
    store = warm_store()

    result = await run_cycle(
        client=ThinqClient(api),
        store=store,
        settings=settings(),
        now=utc(9, 15),
        shutdown=shutdown,
    )

    assert result.skipped == "shutdown"
    stored = store.get_observation(result.sample_id)
    assert stored is None  # nothing, rather than something half-written


async def test_the_loop_stops_when_shutdown_is_requested():
    api = FakeThinqApi(energy_usage=usage("2.150"), device_status=STATE)
    store = warm_store()
    shutdown = asyncio.Event()
    moments = iter([utc(9, 15), utc(9, 20), utc(9, 25), utc(9, 30), utc(9, 35), utc(9, 40)])

    async def wait(_seconds):
        shutdown.set()

    results = await run_loop(
        client=ThinqClient(api),
        store=store,
        settings=settings(),
        shutdown=shutdown,
        clock=lambda: next(moments),
        wait=wait,
    )

    assert len(results) == 1
    assert results[0].wrote


async def test_the_loop_runs_a_bounded_number_of_cycles():
    api = FakeThinqApi(
        energy_usage=sequence(usage("2.100"), usage("2.150"), usage("2.200")),
        device_status=STATE,
    )
    store = warm_store()
    moments = iter([utc(9, 15), utc(9, 15), utc(9, 20), utc(9, 20), utc(9, 25), utc(9, 25)])

    async def wait(_seconds):
        return None

    results = await run_loop(
        client=ThinqClient(api),
        store=store,
        settings=settings(),
        max_cycles=3,
        clock=lambda: next(moments),
        wait=wait,
    )

    assert len(results) == 3
    assert [r.write.action for r in results] == ["created", "created", "created"]


def test_the_loop_sleeps_until_the_next_slot():
    assert seconds_until_next_slot(utc(9, 15), 300) == 300
    assert seconds_until_next_slot(utc(9, 17), 300) == 180
    assert seconds_until_next_slot(utc(9, 14, day=20) + timedelta(seconds=59), 300) == 1


# --- The SDK's date.today() coupling (design D1) ----------------------------


async def test_the_energy_request_uses_the_device_local_day_not_the_process_date():
    """A UTC-clocked container must still be able to ask for Manila's today.

    The SDK's device wrapper compares the requested end date against
    `date.today()` — the *process's* system-local date — and raises for the
    first eight hours of every Manila day. The low-level path this collector
    uses performs no such comparison.
    """
    api = FakeThinqApi(energy_usage=usage("0.021", day="20260821"), device_status=STATE)
    store = warm_store()

    # 16:05 UTC on the 20th is 00:05 on the 21st in Manila.
    result = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(16, 5)
    )

    call = api.calls_to("async_get_device_energy_usage")[0]
    assert call.args[3] == "20260821"  # start_date: Manila's day, not UTC's
    assert call.args[4] == "20260821"
    assert result.observation.local_date.isoformat() == "2026-08-21"
    assert result.observation.energy_source.ok


def test_the_sdk_wrapper_would_have_raised_on_the_same_request(monkeypatch):
    """The coupling is real — this is what we are avoiding, demonstrated."""
    from datetime import date as real_date

    import thinqconnect.devices.connect_device as connect_device

    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return real_date(2026, 8, 20)  # the process clock is still on the 20th

    monkeypatch.setattr(connect_device, "date", FrozenDate)

    wrapper = connect_device.ConnectBaseDevice.__new__(connect_device.ConnectBaseDevice)
    with pytest.raises(ValueError):
        connect_device.ConnectBaseDevice._check_date_format(
            wrapper, "DAILY", "20260821", "20260821"
        )


# --- Structured logging ------------------------------------------------------


async def test_a_cycle_logs_every_field_the_spec_requires(caplog):
    api = FakeThinqApi(energy_usage=usage("2.150"), device_status=STATE)
    store = warm_store()
    await run_cycle(client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 15))

    with caplog.at_level(logging.INFO, logger="airchive.cycle"):
        api.energy_usage = usage("2.200")
        await run_cycle(
            client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 20)
        )

    record = next(r for r in caplog.records if r.getMessage() == "cycle complete")
    context = getattr(record, CONTEXT_KEY)

    for key in (
        "sampleId",
        "deviceId",
        "scheduledAt",
        "observedAt",
        "energyOk",
        "stateOk",
        "previousRawDailyTotal",
        "rawDailyTotal",
        "intervalValue",
        "intervalSeconds",
        "intervalStatus",
        "storagePath",
        "cycleSeconds",
    ):
        assert key in context, f"missing {key}"

    assert context["sampleId"] == "20260820T092000Z"
    assert context["previousRawDailyTotal"] == "2.150"
    assert context["rawDailyTotal"] == "2.200"
    assert context["intervalValue"] == "0.050"
    assert context["storagePath"].endswith("/telemetry/20260820T092000Z")


def test_the_sample_id_ties_a_stored_record_to_its_logs():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        "airchive.cycle", logging.INFO, __file__, 1, "cycle complete", (), None
    )
    setattr(record, CONTEXT_KEY, {"sampleId": "20260820T091500Z"})

    payload = json.loads(formatter.format(record))
    assert payload["sampleId"] == "20260820T091500Z"
    assert payload["severity"] == "INFO"


def test_log_records_carry_no_secrets():
    sentinel = "SENTINEL-PAT-4e1c9a7b2d6f43c8"
    register_secret(sentinel)
    try:
        formatter = JsonFormatter()
        record = logging.LogRecord(
            "airchive.cycle",
            logging.ERROR,
            __file__,
            1,
            "failed with Bearer %s",
            (sentinel,),
            None,
        )
        setattr(record, CONTEXT_KEY, {"headers": {"Authorization": f"Bearer {sentinel}"}})

        rendered = formatter.format(record)
        assert sentinel not in rendered
        assert "<redacted>" in rendered
    finally:
        clear_secrets()


def test_configure_logging_installs_one_json_handler():
    import io

    stream = io.StringIO()
    logger = configure_logging("DEBUG", stream=stream)
    logger.info("hello", extra={CONTEXT_KEY: {"sampleId": "x"}})

    payload = json.loads(stream.getvalue().strip())
    assert payload["message"] == "hello"
    assert payload["sampleId"] == "x"
    assert len(logger.handlers) == 1


# --- Partial success end to end ---------------------------------------------


async def test_a_state_failure_still_produces_a_usable_energy_record():
    api = FakeThinqApi(
        energy_usage=usage("2.150"), device_status=thinq_api_exception("1222", "offline")
    )
    store = warm_store()
    knobs, _ = no_wait()

    result = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 15), **knobs
    )

    document = store.get_observation(result.sample_id)
    assert document["energy"]["rawDailyTotal"] == "2.150"
    assert document["state"] is None
    assert str(QualityFlag.DEVICE_OFFLINE) in document["quality"]["flags"]
    assert str(QualityFlag.PARTIAL_OBSERVATION) in document["quality"]["flags"]
    # A partial cycle is still a successful one for health purposes.
    assert store.get_health()["consecutiveFailures"] == 0


async def test_an_energy_failure_does_not_become_the_next_baseline():
    client = FakeFirestoreClient()
    store = warm_store(client)
    api = FakeThinqApi(energy_usage=usage("2.100"), device_status=STATE)
    await run_cycle(client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 15))

    api.energy_usage = thinq_api_exception("2000", "server error")
    knobs, _ = no_wait()
    await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 20), **knobs
    )

    api.energy_usage = usage("2.200")
    third = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(9, 25)
    )

    document = store.get_observation(third.sample_id)
    # The baseline is the 09:15 reading, not the failed 09:20 one.
    assert document["energy"]["previous"]["sampleId"] == "20260820T091500Z"
    assert document["energy"]["intervalValue"] == "0.100"
    assert Decimal(document["energy"]["intervalValue"]) > 0


async def test_an_implausible_finalized_total_is_recorded_and_left_unresolved():
    client = FakeFirestoreClient()
    store = warm_store(client)
    api = FakeThinqApi(energy_usage=usage("8.732"), device_status=STATE)
    await run_cycle(client=ThinqClient(api), store=store, settings=settings(), now=utc(15, 55))

    def not_ready(_device, _prop, _period, start, _end):
        if start == "20260820":
            return {"unit": "kWh", "energyData": []}
        return {"unit": "kWh", "energyData": [{"date": "20260821", "value": "0.021"}]}

    api.energy_usage = not_ready
    knobs, _ = no_wait()
    rollover = await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(16, 5), **knobs
    )

    # LG later reports a "final" total below a value already observed that day.
    def implausible(_device, _prop, _period, start, _end):
        if start == "20260820":
            return {"unit": "kWh", "energyData": [{"date": "20260820", "value": "8.500"}]}
        return {"unit": "kWh", "energyData": [{"date": "20260821", "value": "0.030"}]}

    api.energy_usage = implausible
    await run_cycle(
        client=ThinqClient(api), store=store, settings=settings(), now=utc(16, 10), **knobs
    )

    document = store.get_observation(rollover.sample_id)
    assert document["quality"]["intervalStatus"] == str(IntervalStatus.DAY_ROLLOVER_UNRESOLVED)
    assert str(QualityFlag.IMPLAUSIBLE_FINAL_TOTAL) in document["quality"]["flags"]
    assert document["energy"]["intervalValue"] is None
    assert document["energy"]["finalPreviousDayTotal"] == "8.500"
    assert store.pending_reconciliations() == {}
