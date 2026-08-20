"""Inspection commands: `latest`, `health`, `anomalies`, `compare`.

These exist so routine operational questions are answered from a terminal
instead of by clicking through a console, and so a future agent inheriting this
repository gets executable affordances rather than prose. All four are
read-only; `compare` in particular writes nothing to the telemetry series.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from airchive.config import ConfigError, load_config, load_inspection_config
from airchive.observation.state import principal_fields
from airchive.redaction import register_secret, scrub_object
from airchive.storage.client import build_client
from airchive.storage.store import TelemetryStore
from airchive.thinq.client import open_client
from airchive.thinq.failures import ThinqRequestError
from airchive.thinq.payloads import extract_energy_reading


def _open_store() -> tuple[TelemetryStore, str, Any, str]:
    project_id, device_id, tz, tz_name = load_inspection_config()
    return TelemetryStore(build_client(project_id), device_id), device_id, tz, tz_name


def _instant(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    return "—" if value is None else str(value)


def _source_summary(document: dict[str, Any]) -> str:
    source = document.get("source") or {}
    parts = []
    for name in ("energy", "state"):
        outcome = source.get(name) or {}
        if outcome.get("ok"):
            parts.append(f"{name}=ok")
        else:
            parts.append(f"{name}={outcome.get('failureClass') or 'failed'}")
    return " ".join(parts)


def _observation_row(document: dict[str, Any]) -> str:
    energy = document.get("energy") or {}
    quality = document.get("quality") or {}
    seconds = energy.get("intervalSeconds")
    flags = ",".join(quality.get("flags") or []) or "—"
    return (
        f"{document.get('sampleId', '—'):<18} "
        f"{_instant(document.get('observedAt')):<21} "
        f"{str(energy.get('rawDailyTotal') or '—'):>10} "
        f"{str(energy.get('intervalValue') or '—'):>10} "
        f"{(f'{seconds:.0f}s' if seconds is not None else '—'):>7}  "
        f"{quality.get('intervalStatus', '—'):<24} "
        f"{_source_summary(document):<28} {flags}"
    )


_HEADER = (
    f"{'sampleId':<18} {'observedAt':<21} {'raw':>10} {'interval':>10} {'dur':>7}  "
    f"{'intervalStatus':<24} {'sources':<28} flags"
)


def latest(limit: int = 10, *, store: TelemetryStore | None = None) -> int:
    try:
        store, device_id = (store, store.device_id) if store else _open_store()[:2]
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    documents = store.latest_observations(limit=limit)
    if not documents:
        print(f"No observations stored for {device_id} yet.")
        return 0

    print(f"device: {device_id}   showing {len(documents)} most recent\n")
    print(_HEADER)
    for document in documents:
        print(_observation_row(document))

    newest = documents[0]
    print(f"\nnewest storage path: devices/{device_id}/telemetry/{newest.get('sampleId')}")
    return 0


def health(*, store: TelemetryStore | None = None) -> int:
    try:
        store, device_id = (store, store.device_id) if store else _open_store()[:2]
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    record = store.get_health()
    if not record:
        # Still fall through to the reconciliation queue below: hiding it behind
        # a missing health record would hide exactly the state worth seeing.
        print(f"No health record for {device_id} yet — the collector has not run.")
        record = {}

    print(f"device:              {device_id}")
    print(f"collectorVersion:    {record.get('collectorVersion') or '—'}")
    print(f"lastAttemptAt:       {_instant(record.get('lastAttemptAt'))}")
    print(f"lastSuccessAt:       {_instant(record.get('lastSuccessAt'))}")
    print(f"lastSampleId:        {record.get('lastSampleId') or '—'}")
    print(f"lastSamplePath:      {record.get('lastSamplePath') or '—'}")
    print(f"lastErrorAt:         {_instant(record.get('lastErrorAt'))}")
    print(f"lastErrorClass:      {record.get('lastErrorClass') or '—'}")
    print(f"lastErrorMessage:    {record.get('lastErrorMessage') or '—'}")
    print(f"consecutiveFailures: {record.get('consecutiveFailures') or 0}")

    if record.get("consecutiveRateLimits"):
        print(f"consecutiveRateLimits: {record['consecutiveRateLimits']}")
        print(f"rateLimitedUntil:    {_instant(record.get('rateLimitedUntil'))}")
    if record.get("leaseUntil"):
        print(f"leaseUntil:          {_instant(record.get('leaseUntil'))}")

    pending = store.pending_reconciliations()
    print(f"pendingReconciliations: {len(pending)}")
    for sample_id, entry in sorted(pending.items()):
        print(f"  {sample_id} awaiting {entry.get('previousLocalDate')}")
    return 0


def _parse_instant(text: str | None, default: datetime) -> datetime:
    if not text:
        return default
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def anomalies(
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    *,
    store: TelemetryStore | None = None,
    now: datetime | None = None,
) -> int:
    try:
        store, device_id = (store, store.device_id) if store else _open_store()[:2]
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    now = now or datetime.now(UTC)
    try:
        start = _parse_instant(since, now - timedelta(hours=24))
        end = _parse_instant(until, now + timedelta(minutes=1))
    except ValueError as exc:
        print(f"Could not read the time range: {exc}", file=sys.stderr)
        return 2

    documents = store.observations_in_range(start, end, limit=max(limit * 20, 500))
    flagged = [d for d in documents if TelemetryStore.is_anomalous(d)][:limit]

    print(
        f"device: {device_id}   range: {_instant(start)} .. {_instant(end)}   "
        f"scanned {len(documents)}, flagged {len(flagged)}\n"
    )
    if not flagged:
        print("No anomalous observations in that range.")
        return 0

    print(_HEADER)
    for document in flagged:
        print(_observation_row(document))
    return 0


async def _compare(
    *, store: TelemetryStore | None = None, client: Any | None = None
) -> int:
    try:
        project_id, device_id, tz, tz_name = load_inspection_config()
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    register_secret(config.thinq.pat)
    store = store or TelemetryStore(build_client(project_id), device_id)
    device_id = store.device_id

    documents = store.latest_observations(limit=1)
    stored = documents[0] if documents else None
    if stored is None:
        print("Nothing stored yet — run `airchive poll --once` first.")

    local_now = datetime.now(tz)
    if client is not None:
        live_energy, live_state = await _read_live(client, device_id, config, local_now)
    else:
        async with open_client(config.thinq) as opened:
            live_energy, live_state = await _read_live(opened, device_id, config, local_now)

    reading = (
        extract_energy_reading(
            live_energy, config.thinq.energy_property, day_label=local_now.date().isoformat()
        )
        if live_energy is not None
        else None
    )

    stored_energy = (stored or {}).get("energy") or {}
    print(f"\ndevice: {device_id}   local day: {local_now.date().isoformat()} ({tz_name})\n")
    print(f"{'field':<28} {'stored':<28} live")
    stored_sample_id = str((stored or {}).get("sampleId") or "—")
    print(f"{'sampleId':<28} {stored_sample_id:<28} (live reading, unstored)")
    print(
        f"{'observedAt':<28} {_instant((stored or {}).get('observedAt')):<28} "
        f"{_instant(local_now)}"
    )
    print(
        f"{'rawDailyTotal':<28} {str(stored_energy.get('rawDailyTotal') or '—'):<28} "
        f"{str(reading.value) if reading else '—'}"
    )
    print(
        f"{'unit':<28} {str(stored_energy.get('unit') or '—'):<28} "
        f"{(reading.unit if reading and reading.unit else '—')}"
    )

    stored_state = principal_fields((stored or {}).get("state") or {})
    live_fields = principal_fields(live_state or {})
    for key in sorted(set(stored_state) | set(live_fields)):
        mark = " " if stored_state.get(key) == live_fields.get(key) else "*"
        print(
            f"{mark}{key:<27} {str(stored_state.get(key, '—')):<28} "
            f"{live_fields.get(key, '—')}"
        )

    print("\n(* differs. This command wrote nothing to the telemetry series.)")
    if live_state is not None:
        print("\nfull live state:")
        print(json.dumps(scrub_object(live_state), indent=2, ensure_ascii=False, default=str))
    return 0


async def _read_live(client: Any, device_id: str, config: Any, local_now: datetime):
    """Read energy and state without letting either failure hide the other."""
    try:
        live_energy = await client.get_daily_energy_usage(
            device_id, config.thinq.energy_property, local_now.date()
        )
    except ThinqRequestError as exc:
        live_energy = None
        print(f"live energy: FAILED — {exc.failure}")
    try:
        live_state = await client.get_device_status(device_id)
    except ThinqRequestError as exc:
        live_state = None
        print(f"live state:  FAILED — {exc.failure}")
    return live_energy, live_state


def compare(*, store: TelemetryStore | None = None, client: Any | None = None) -> int:
    return asyncio.run(_compare(store=store, client=client))
