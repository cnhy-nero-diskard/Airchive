"""One polling cycle, end to end.

A cycle issues exactly two requests on the routine path — the current-day energy
counter and the device state — and produces exactly one classified observation,
or none. Nothing it can encounter is allowed to end the collector: a malformed
response, an unexpected exception, and a Firestore failure all end *this* cycle
and leave the next one free to run.

Every invocation reconstructs what it needs from Firestore, so a cold start on a
scheduled job behaves identically to the hundredth cycle of a long-running one.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from airchive.config import CollectorConfig, ConfigError
from airchive.logging_setup import CONTEXT_KEY, get_logger
from airchive.observation.build import RequestResult, build_observation
from airchive.observation.model import IntervalStatus, Observation, QualityFlag
from airchive.runtime.retry import (
    AttemptLog,
    RetryPolicy,
    call_with_retries,
    rate_limit_cooldown_seconds,
)
from airchive.storage.ids import floor_to_slot, sample_id_of_slot
from airchive.storage.store import DEFAULT_LEASE_SECONDS, TelemetryStore, WriteOutcome
from airchive.thinq.client import ThinqClient
from airchive.thinq.failures import FailureClass, ThinqFailure, ThinqRequestError
from airchive.thinq.payloads import extract_energy_reading, supported_energy_properties
from airchive.thinq.validation import check_energy_property_supported

logger = get_logger("cycle")


@dataclass(frozen=True)
class CycleSettings:
    device_id: str
    energy_property: str
    timezone: ZoneInfo
    timezone_name: str
    interval_seconds: int = 300
    collector_version: str = "0.0.0"
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    lease_seconds: int = DEFAULT_LEASE_SECONDS

    @classmethod
    def from_config(cls, config: CollectorConfig, collector_version: str) -> CycleSettings:
        return cls(
            device_id=config.thinq.device_id,
            energy_property=config.thinq.energy_property,
            timezone=config.day_timezone,
            timezone_name=config.day_timezone_name,
            interval_seconds=config.poll_interval_seconds,
            collector_version=collector_version,
            # Retries must not push a cycle past its own interval.
            retry_policy=RetryPolicy(
                total_budget_seconds=min(45.0, config.poll_interval_seconds * 0.5)
            ),
            lease_seconds=max(60, int(config.poll_interval_seconds * 0.8)),
        )


@dataclass
class CycleResult:
    sample_id: str
    scheduled_at: datetime
    observed_at: datetime
    skipped: str | None = None
    write: WriteOutcome | None = None
    observation: Observation | None = None
    error: str | None = None
    duration_seconds: float = 0.0
    api_calls: int = 0

    @property
    def wrote(self) -> bool:
        return bool(self.write and self.write.wrote)


def _cooldown_failure(until: datetime) -> ThinqFailure:
    return ThinqFailure(
        failure_class=FailureClass.RATE_LIMITED,
        error_name="RateLimitCooldown",
        safe_message=f"no request issued; rate-limit cooldown until {until.isoformat()}",
    )


async def _ensure_metadata(
    client: ThinqClient, store: TelemetryStore, settings: CycleSettings, now: datetime
) -> tuple[str | None, int]:
    """Return the active metadata version, fetching profiles only when cold.

    Warm metadata means a routine cycle issues no device-list, device-profile, or
    energy-profile request at all — the per-cycle call budget depends on this.
    """
    current = store.get_current_metadata()
    if current and current.get("version"):
        check_energy_property_supported(
            settings.energy_property, list(current.get("supportedEnergyProperties") or [])
        )
        return str(current["version"]), 0

    profile = await client.get_device_profile(settings.device_id)
    energy_profile = await client.get_energy_profile(settings.device_id)
    supported = supported_energy_properties(energy_profile)
    check_energy_property_supported(settings.energy_property, supported)

    version = now.strftime("%Y%m%dT%H%M%SZ")
    store.put_metadata(
        version=version,
        profile=profile,
        energy_profile=energy_profile,
        supported_energy_properties=supported,
        retrieved_at=now,
    )
    return version, 2


async def _fetch_energy(
    client: ThinqClient,
    settings: CycleSettings,
    local_date: date,
    attempts: AttemptLog,
    **retry_kwargs,
) -> RequestResult:
    async def operation():
        return await client.get_daily_energy_usage(
            settings.device_id, settings.energy_property, local_date
        )

    try:
        payload = await call_with_retries(
            operation, settings.retry_policy, log=attempts, **retry_kwargs
        )
    except ThinqRequestError as error:
        return RequestResult.failed(error.failure)

    reading = extract_energy_reading(
        payload, settings.energy_property, day_label=local_date.isoformat()
    )
    if reading is None:
        # Transported fine, but there is no number in it. Never call that zero.
        return RequestResult.failed(
            ThinqFailure(
                failure_class=FailureClass.MALFORMED,
                error_name="NoEnergyReading",
                safe_message="no numeric reading found in the energy response",
            )
        )
    return RequestResult.success(payload, value=reading.value, unit=reading.unit)


async def _fetch_state(
    client: ThinqClient, settings: CycleSettings, attempts: AttemptLog, **retry_kwargs
) -> RequestResult:
    async def operation():
        return await client.get_device_status(settings.device_id)

    try:
        payload = await call_with_retries(
            operation, settings.retry_policy, log=attempts, **retry_kwargs
        )
    except ThinqRequestError as error:
        return RequestResult.failed(error.failure)
    return RequestResult.success(payload)


async def _finalized_total(
    client: ThinqClient,
    store: TelemetryStore,
    settings: CycleSettings,
    day: date,
    now: datetime,
) -> tuple[Decimal | None, int]:
    """LG's finalized total for `day`, from cache or one extra request."""
    cached = store.get_daily_total(day.isoformat())
    if cached is not None:
        return cached, 0

    try:
        payload = await client.get_daily_energy_usage(
            settings.device_id, settings.energy_property, day
        )
    except ThinqRequestError:
        return None, 1

    reading = extract_energy_reading(
        payload, settings.energy_property, day_label=day.isoformat()
    )
    if reading is None:
        return None, 1

    store.put_daily_total(
        day.isoformat(), reading.value, unit=reading.unit, raw=payload, fetched_at=now
    )
    return reading.value, 1


async def run_cycle(
    *,
    client: ThinqClient,
    store: TelemetryStore,
    settings: CycleSettings,
    now: datetime | None = None,
    shutdown: asyncio.Event | None = None,
    **retry_kwargs: Any,
) -> CycleResult:
    """Run one cycle. Never raises: every failure ends up in the result and health."""
    started = time.perf_counter()
    now = now or datetime.now(UTC)
    local_now = now.astimezone(settings.timezone)
    local_date = local_now.date()
    slot = floor_to_slot(now, settings.interval_seconds)
    sample_id = sample_id_of_slot(slot)
    holder = uuid.uuid4().hex[:12]

    result = CycleResult(sample_id=sample_id, scheduled_at=slot, observed_at=now)

    try:
        if not store.acquire_lease(now=now, holder=holder, seconds=settings.lease_seconds):
            # Another cycle is still running. Any gap is handled by the
            # coarse-interval behavior, which has to work anyway.
            result.skipped = "lease"
            logger.warning(
                "cycle skipped: another cycle holds the lease",
                extra={CONTEXT_KEY: {"sampleId": sample_id, "skipped": "lease"}},
            )
            return result
    except Exception as exc:  # noqa: BLE001 — storage failure must not end collection
        result.error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "cycle failed acquiring the lease",
            extra={CONTEXT_KEY: {"sampleId": sample_id, "error": result.error}},
        )
        return result

    try:
        result = await _run_cycle_body(
            client=client,
            store=store,
            settings=settings,
            now=now,
            local_date=local_date,
            result=result,
            shutdown=shutdown,
            started=started,
            **retry_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — containment is the whole point
        result.error = f"{type(exc).__name__}: {exc}"
        # Configuration that the device contradicts is fatal, not transient, and
        # has to look different in health from a flaky network.
        failure_class = "CONFIG_FATAL" if isinstance(exc, ConfigError) else "CYCLE_ERROR"
        try:
            store.record_failure(
                now=now,
                failure_class=failure_class,
                message=result.error,
                collector_version=settings.collector_version,
            )
        except Exception:  # noqa: BLE001 — health is best-effort at this point
            pass
        logger.error(
            "cycle failed",
            extra={CONTEXT_KEY: {"sampleId": sample_id, "error": result.error}},
        )
    finally:
        try:
            store.release_lease(holder)
        except Exception:  # noqa: BLE001
            pass
        result.duration_seconds = time.perf_counter() - started

    return result


async def _run_cycle_body(
    *,
    client: ThinqClient,
    store: TelemetryStore,
    settings: CycleSettings,
    now: datetime,
    local_date: date,
    result: CycleResult,
    shutdown: asyncio.Event | None,
    started: float,
    **retry_kwargs: Any,
) -> CycleResult:
    store.record_attempt(now, settings.collector_version)
    health = store.get_health() or {}

    energy_attempts, state_attempts = AttemptLog(), AttemptLog()
    metadata_version: str | None = None

    cooldown_until = health.get("rateLimitedUntil")
    in_cooldown = cooldown_until is not None and cooldown_until > now

    if in_cooldown:
        # Sustained rate limiting: issue nothing at all this cycle. That is what
        # actually reduces the request rate rather than merely re-pacing it.
        failure = _cooldown_failure(cooldown_until)
        energy = RequestResult.failed(failure)
        state = RequestResult.failed(failure)
    else:
        metadata_version, calls = await _ensure_metadata(client, store, settings, now)
        result.api_calls += calls

        energy = await _fetch_energy(
            client, settings, local_date, energy_attempts, **retry_kwargs
        )
        state = await _fetch_state(client, settings, state_attempts, **retry_kwargs)
        result.api_calls += 2

    observed_at = now
    previous, has_prior = store.find_previous_reading(result.sample_id)

    final_total: Decimal | None = None
    day_gap = (local_date - previous.local_date).days if previous else 0
    if previous is not None and day_gap == 1 and energy.ok:
        final_total, calls = await _finalized_total(
            client, store, settings, previous.local_date, now
        )
        result.api_calls += calls

    observation = build_observation(
        sample_id=result.sample_id,
        device_id=settings.device_id,
        scheduled_at=result.scheduled_at,
        observed_at=observed_at,
        local_date=local_date,
        timezone_name=settings.timezone_name,
        energy_property=settings.energy_property,
        energy=energy,
        state=state,
        previous=previous,
        has_prior_observation=has_prior,
        final_previous_day_total=final_total,
        nominal_interval_seconds=settings.interval_seconds,
        metadata_version=metadata_version or (health.get("metadataVersion") if health else None),
        collector_version=settings.collector_version,
    )
    result.observation = observation

    if shutdown is not None and shutdown.is_set():
        # Abandon before writing rather than leave a half-written cycle behind.
        result.skipped = "shutdown"
        logger.warning(
            "cycle abandoned before writing: shutdown requested",
            extra={CONTEXT_KEY: {"sampleId": result.sample_id, "skipped": "shutdown"}},
        )
        return result

    result.write = store.write_observation(observation, now)

    _update_rate_limit_state(store, observation, health, now)

    if observation.quality.interval_status is IntervalStatus.DAY_ROLLOVER_UNRESOLVED and previous:
        store.enqueue_reconciliation(
            sample_id=result.sample_id,
            previous_local_date=previous.local_date.isoformat(),
            now=now,
        )

    result.api_calls += await _reconcile_pending(
        client=client,
        store=store,
        settings=settings,
        now=now,
        allow_fetch=final_total is None and not in_cooldown,
    )

    if energy.ok or state.ok:
        store.record_success(
            now=now,
            sample_id=result.sample_id,
            path=result.write.path,
            collector_version=settings.collector_version,
        )
    else:
        failure = energy.failure or state.failure
        store.record_failure(
            now=now,
            failure_class=str(failure.failure_class) if failure else None,
            message=failure.safe_message if failure else None,
            collector_version=settings.collector_version,
        )

    result.duration_seconds = time.perf_counter() - started
    logger.info(
        "cycle complete",
        extra={CONTEXT_KEY: _log_context(result, observation, previous, energy, state)},
    )
    return result


def _update_rate_limit_state(
    store: TelemetryStore, observation: Observation, health: dict[str, Any], now: datetime
) -> None:
    """Track sustained rate limiting so the next cycles back off across invocations."""
    rate_limited = observation.quality.has(QualityFlag.RATE_LIMITED)
    previous_count = int(health.get("consecutiveRateLimits") or 0)

    if rate_limited:
        count = previous_count + 1
        cooldown = rate_limit_cooldown_seconds(count)
        store.health_ref.set(
            {
                "consecutiveRateLimits": count,
                "rateLimitedUntil": now + timedelta(seconds=cooldown),
            },
            merge=True,
        )
    elif previous_count:
        store.health_ref.set(
            {"consecutiveRateLimits": 0, "rateLimitedUntil": None}, merge=True
        )


async def _reconcile_pending(
    *,
    client: ThinqClient,
    store: TelemetryStore,
    settings: CycleSettings,
    now: datetime,
    allow_fetch: bool,
) -> int:
    """Fill in or abandon deferred rollovers. At most one extra request."""
    pending = store.pending_reconciliations()
    if not pending:
        return 0

    api_calls = 0
    for sample_id in sorted(pending):
        entry = pending[sample_id]
        enqueued_at = entry.get("enqueuedAt")
        previous_local_date = entry.get("previousLocalDate")
        if not previous_local_date:
            store.dequeue_reconciliation(sample_id)
            continue

        if enqueued_at is not None and now - enqueued_at >= timedelta(hours=24):
            # Permanently unresolved is honest. A fabricated value is not.
            store.dequeue_reconciliation(sample_id)
            logger.warning(
                "reconciliation abandoned after its window",
                extra={
                    CONTEXT_KEY: {
                        "sampleId": sample_id,
                        "previousLocalDate": previous_local_date,
                    }
                },
            )
            continue

        day = date.fromisoformat(previous_local_date)
        total = store.get_daily_total(previous_local_date)
        if total is None and allow_fetch and api_calls == 0:
            total, calls = await _finalized_total(client, store, settings, day, now)
            api_calls += calls
        if total is None:
            continue

        stored = store.get_observation(sample_id)
        if stored is None:
            store.dequeue_reconciliation(sample_id)
            continue

        previous_value = ((stored.get("energy") or {}).get("previous") or {}).get(
            "rawDailyTotal"
        )
        current_value = (stored.get("energy") or {}).get("rawDailyTotal")
        if previous_value is None or current_value is None:
            store.dequeue_reconciliation(sample_id)
            continue

        previous_decimal = Decimal(str(previous_value))
        if total < previous_decimal:
            # Below a value already observed that day, so the reconstruction
            # cannot be performed. Record what LG returned and stop retrying;
            # permanently unresolved is honest, a fabricated number is not.
            store.mark_implausible_final_total(
                sample_id=sample_id, final_previous_day_total=total, now=now
            )
            store.dequeue_reconciliation(sample_id)
            logger.warning(
                "finalized previous-day total is implausible; leaving the rollover unresolved",
                extra={
                    CONTEXT_KEY: {
                        "sampleId": sample_id,
                        "finalPreviousDayTotal": str(total),
                        "previousRawDailyTotal": str(previous_decimal),
                    }
                },
            )
            continue

        interval = (total - previous_decimal) + Decimal(str(current_value))
        store.apply_reconciliation(
            sample_id=sample_id,
            interval_value=interval,
            final_previous_day_total=total,
            now=now,
        )
        store.dequeue_reconciliation(sample_id)
        logger.info(
            "reconciled a deferred rollover",
            extra={CONTEXT_KEY: {"sampleId": sample_id, "intervalValue": str(interval)}},
        )

    return api_calls


def _log_context(
    result: CycleResult,
    observation: Observation,
    previous,
    energy: RequestResult,
    state: RequestResult,
) -> dict[str, Any]:
    """Everything the runtime spec requires a cycle's logs to carry."""
    return {
        "sampleId": result.sample_id,
        "deviceId": observation.device_id,
        "scheduledAt": result.scheduled_at.isoformat(),
        "observedAt": result.observed_at.isoformat(),
        "localDate": observation.local_date.isoformat(),
        "energyOk": energy.ok,
        "energyFailureClass": str(energy.failure.failure_class) if energy.failure else None,
        "stateOk": state.ok,
        "stateFailureClass": str(state.failure.failure_class) if state.failure else None,
        "previousRawDailyTotal": str(previous.value) if previous else None,
        "rawDailyTotal": str(observation.raw_daily_total)
        if observation.raw_daily_total is not None
        else None,
        "intervalValue": str(observation.interval_value)
        if observation.interval_value is not None
        else None,
        "intervalSeconds": observation.interval_seconds,
        "intervalStatus": str(observation.quality.interval_status),
        "flags": [str(flag) for flag in observation.quality.flags],
        "writeAction": result.write.action if result.write else None,
        "storagePath": result.write.path if result.write else None,
        "apiCalls": result.api_calls,
        "cycleSeconds": round(result.duration_seconds, 6),
    }
