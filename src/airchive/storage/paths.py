"""The Firestore layout (design D8).

    devices/{deviceId}                     identity, alias, model, metadata pointer
      telemetry/{sampleId}                 immutable observation series  <- source of truth
      dailyTotals/{localDate}              LG's finalized per-day totals (cached)
      metadata/{profileVersion}            versioned profile + energy-profile snapshots
      metadata/current                     pointer to the active profile version
      runtime/collector                    mutable health, overwritten in place
      runtime/reconciliation               bounded queue awaiting a finalized total

`dailyTotals` is deliberate rather than incidental: the finalized previous-day
total is fetched once and reused, which both keeps the per-cycle call budget and
leaves a clean daily ledger to reconcile the 5-minute series against later.
"""

from __future__ import annotations

DEVICES = "devices"
TELEMETRY = "telemetry"
DAILY_TOTALS = "dailyTotals"
METADATA = "metadata"
RUNTIME = "runtime"

CURRENT_METADATA_ID = "current"
COLLECTOR_HEALTH_ID = "collector"
RECONCILIATION_ID = "reconciliation"


def device_path(device_id: str) -> str:
    return f"{DEVICES}/{device_id}"


def telemetry_path(device_id: str, sample_id: str) -> str:
    return f"{device_path(device_id)}/{TELEMETRY}/{sample_id}"


def daily_total_path(device_id: str, local_date: str) -> str:
    return f"{device_path(device_id)}/{DAILY_TOTALS}/{local_date}"


def metadata_path(device_id: str, version: str) -> str:
    return f"{device_path(device_id)}/{METADATA}/{version}"


def health_path(device_id: str) -> str:
    return f"{device_path(device_id)}/{RUNTIME}/{COLLECTOR_HEALTH_ID}"


def reconciliation_path(device_id: str) -> str:
    return f"{device_path(device_id)}/{RUNTIME}/{RECONCILIATION_ID}"
