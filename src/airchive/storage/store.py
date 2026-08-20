"""Reading and writing the telemetry series.

Deliberately not an abstraction layer over Firestore — there is one database and
it is not going to be swapped. What this module does provide is the *rules* that
keep a years-long series trustworthy: deterministic identity, transactional
precedence so a stale retry cannot degrade a better record, raw payloads kept
out of the index, health kept out of history, and reconciliation bounded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from airchive.observation.model import (
    ANOMALOUS_FLAGS,
    ANOMALOUS_STATUSES,
    IntervalStatus,
    Observation,
    PreviousReading,
    QualityFlag,
    decimal_fields,
)
from airchive.storage import paths

#: The stored mirror of the document ID. Ordering keys on this rather than on
#: `__name__` because Firestore refuses descending key scans; the value is
#: identical, so the chronological-sort property of design D6 still holds.
SAMPLE_ID_FIELD = "sampleId"

#: How far back the previous-reading lookup will scan (design D12).
PREVIOUS_LOOKUP_LIMIT = 12

#: How long an unresolved rollover stays eligible for reconciliation (design D11).
RECONCILIATION_WINDOW = timedelta(hours=24)

#: How long a cycle holds the advisory lease (design D14).
DEFAULT_LEASE_SECONDS = 240


@dataclass(frozen=True)
class WriteOutcome:
    """What a write actually did, so the caller can log the truth about it."""

    action: str  # created | upgraded | unchanged | refused
    path: str
    existing_completeness: int | None = None
    new_completeness: int | None = None

    @property
    def wrote(self) -> bool:
        return self.action in ("created", "upgraded")


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _firestore_transaction_runner(client: Any) -> Callable[[Callable], Any]:
    """Run a function inside a real Firestore transaction.

    Kept behind a seam so the test doubles can supply their own runner without
    reimplementing the SDK's transaction internals.
    """

    def run(fn: Callable[[Any], Any]) -> Any:
        from google.cloud import firestore

        @firestore.transactional
        def _wrapped(transaction):
            return fn(transaction)

        return _wrapped(client.transaction())

    return run


class TelemetryStore:
    """All Firestore access for one device."""

    def __init__(self, client: Any, device_id: str):
        self._client = client
        self.device_id = device_id
        runner = getattr(client, "run_transaction", None)
        self._run_transaction: Callable[[Callable], Any] = (
            runner if callable(runner) else _firestore_transaction_runner(client)
        )

    # --- references ---------------------------------------------------------

    @property
    def device_ref(self):
        return self._client.document(paths.device_path(self.device_id))

    @property
    def telemetry_ref(self):
        return self._client.collection(
            f"{paths.device_path(self.device_id)}/{paths.TELEMETRY}"
        )

    def observation_ref(self, sample_id: str):
        return self._client.document(paths.telemetry_path(self.device_id, sample_id))

    def daily_total_ref(self, local_date: str):
        return self._client.document(paths.daily_total_path(self.device_id, local_date))

    def metadata_ref(self, version: str):
        return self._client.document(paths.metadata_path(self.device_id, version))

    @property
    def health_ref(self):
        return self._client.document(paths.health_path(self.device_id))

    @property
    def reconciliation_ref(self):
        return self._client.document(paths.reconciliation_path(self.device_id))

    # --- observations -------------------------------------------------------

    def write_observation(self, observation: Observation, now: datetime) -> WriteOutcome:
        """Write one slot idempotently, with completeness precedence (design D7).

        Deterministic identifiers stop duplicate *creation* but open a stale
        overwrite hazard: a delayed retry for slot T could clobber a better
        record written since. Precedence closes it, which is what makes replay
        safe on a runtime that occasionally double-delivers.
        """
        ref = self.observation_ref(observation.sample_id)
        document = observation.to_document()
        document["persistedAt"] = now
        new_completeness = observation.completeness

        def _txn(transaction) -> WriteOutcome:
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                transaction.set(ref, document)
                return WriteOutcome("created", ref.path, None, new_completeness)

            existing = snapshot.to_dict() or {}
            existing_completeness = int(existing.get("completeness") or 0)

            if new_completeness > existing_completeness:
                transaction.set(ref, document)
                return WriteOutcome(
                    "upgraded", ref.path, existing_completeness, new_completeness
                )
            if new_completeness == existing_completeness:
                # First writer wins: rewriting would churn observedAt for nothing.
                return WriteOutcome(
                    "unchanged", ref.path, existing_completeness, new_completeness
                )
            return WriteOutcome("refused", ref.path, existing_completeness, new_completeness)

        return self._run_transaction(_txn)

    def get_observation(self, sample_id: str) -> dict[str, Any] | None:
        snapshot = self.observation_ref(sample_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def latest_observations(self, limit: int = 10) -> list[dict[str, Any]]:
        """Most recent observations first.

        Ordered by the mirrored `sampleId` field, not by the document key:
        Firestore rejects a descending key scan outright ("Firestore does not
        support descending key scans"), which the emulator confirms. The field
        holds the same lexicographically-chronological value (design D6), and
        its automatic single-field index covers this — so the property the
        design relies on holds, just through the field rather than the key.
        """
        query = self.telemetry_ref.order_by(SAMPLE_ID_FIELD, direction="DESCENDING").limit(limit)
        return [snapshot.to_dict() for snapshot in query.stream()]

    def find_previous_reading(
        self, before_sample_id: str, limit: int = PREVIOUS_LOOKUP_LIMIT
    ) -> tuple[PreviousReading | None, bool]:
        """The newest usable baseline before `before_sample_id` (design D12).

        Returns `(reading, any_prior_observation_exists)`. The second value is
        what separates "nothing was ever recorded" from "something was recorded
        but carries no usable energy" — they look alike and mean different
        things about collector health.

        Bounded on purpose: an unbounded walk after a long outage would scan far
        and produce a meaninglessly wide interval anyway.
        """
        query = (
            self.telemetry_ref.order_by(SAMPLE_ID_FIELD, direction="DESCENDING")
            .start_after([before_sample_id])
            .limit(limit)
        )

        any_prior = False
        for snapshot in query.stream():
            any_prior = True
            reading = self._as_previous_reading(snapshot.to_dict() or {})
            if reading is not None:
                return reading, True
        return None, any_prior

    @staticmethod
    def _as_previous_reading(document: dict[str, Any]) -> PreviousReading | None:
        energy = document.get("energy") or {}
        source = (document.get("source") or {}).get("energy") or {}
        if not source.get("ok"):
            return None

        value = _to_decimal(energy.get("rawDailyTotal"))
        observed_at = document.get("observedAt")
        local_date = document.get("localDate")
        sample_id = document.get("sampleId")
        if value is None or not observed_at or not local_date or not sample_id:
            return None

        return PreviousReading(
            sample_id=str(sample_id),
            observed_at=observed_at,
            local_date=date.fromisoformat(str(local_date)),
            value=value,
        )

    def observations_in_range(
        self, since: datetime, until: datetime, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Observations with `observedAt` in `[since, until)`.

        A range filter and an order on the same field, so the automatic
        single-field index covers it — no composite index is required.
        """
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = (
            self.telemetry_ref.where(filter=FieldFilter("observedAt", ">=", since))
            .where(filter=FieldFilter("observedAt", "<", until))
            .order_by("observedAt", direction="DESCENDING")
            .limit(limit)
        )
        return [snapshot.to_dict() for snapshot in query.stream()]

    @staticmethod
    def is_anomalous(document: dict[str, Any]) -> bool:
        quality = document.get("quality") or {}
        status = quality.get("intervalStatus")
        flags = quality.get("flags") or []
        if status in {str(s) for s in ANOMALOUS_STATUSES}:
            return True
        return any(flag in {str(f) for f in ANOMALOUS_FLAGS} for flag in flags)

    # --- daily totals -------------------------------------------------------

    def get_daily_total(self, local_date: str) -> Decimal | None:
        snapshot = self.daily_total_ref(local_date).get()
        if not snapshot.exists:
            return None
        return _to_decimal((snapshot.to_dict() or {}).get("total"))

    def put_daily_total(
        self,
        local_date: str,
        total: Decimal,
        *,
        unit: str | None,
        raw: Any,
        fetched_at: datetime,
    ) -> None:
        """Cache LG's finalized total for a day. Fetched once, reused all day."""
        self.daily_total_ref(local_date).set(
            {
                "localDate": local_date,
                **decimal_fields("total", total),
                "unit": unit,
                "raw": raw,
                "fetchedAt": fetched_at,
                "deviceId": self.device_id,
            }
        )

    # --- metadata -----------------------------------------------------------

    def get_current_metadata(self) -> dict[str, Any] | None:
        snapshot = self.metadata_ref(paths.CURRENT_METADATA_ID).get()
        return snapshot.to_dict() if snapshot.exists else None

    def put_metadata(
        self,
        *,
        version: str,
        profile: Any,
        energy_profile: Any,
        supported_energy_properties: list[str],
        retrieved_at: datetime,
    ) -> str:
        """Store a profile snapshot under its version and point `current` at it.

        Versioned rather than overwritten so a historical observation can still
        be interpreted against the profile that was in effect when it was taken.
        """
        self.metadata_ref(version).set(
            {
                "version": version,
                "deviceId": self.device_id,
                "profile": profile,
                "energyProfile": energy_profile,
                "supportedEnergyProperties": supported_energy_properties,
                "retrievedAt": retrieved_at,
            }
        )
        self.metadata_ref(paths.CURRENT_METADATA_ID).set(
            {
                "version": version,
                "supportedEnergyProperties": supported_energy_properties,
                "updatedAt": retrieved_at,
            }
        )
        return version

    def put_device_record(
        self, *, alias: str | None, model_name: str | None, device_type: str | None, now: datetime
    ) -> None:
        self.device_ref.set(
            {
                "deviceId": self.device_id,
                "alias": alias,
                "modelName": model_name,
                "deviceType": device_type,
                "updatedAt": now,
            },
            merge=True,
        )

    # --- health -------------------------------------------------------------

    def get_health(self) -> dict[str, Any] | None:
        snapshot = self.health_ref.get()
        return snapshot.to_dict() if snapshot.exists else None

    def record_attempt(self, now: datetime, collector_version: str | None = None) -> None:
        self.health_ref.set(
            {"lastAttemptAt": now, "collectorVersion": collector_version}, merge=True
        )

    def record_success(
        self, *, now: datetime, sample_id: str, path: str, collector_version: str | None = None
    ) -> None:
        self.health_ref.set(
            {
                "lastSuccessAt": now,
                "lastSampleId": sample_id,
                "lastSamplePath": path,
                "consecutiveFailures": 0,
                "collectorVersion": collector_version,
            },
            merge=True,
        )

    def record_failure(
        self,
        *,
        now: datetime,
        failure_class: str | None,
        message: str | None,
        collector_version: str | None = None,
    ) -> int:
        """Bump the consecutive-failure count and record the last error."""

        def _txn(transaction) -> int:
            snapshot = self.health_ref.get(transaction=transaction)
            existing = (snapshot.to_dict() or {}) if snapshot.exists else {}
            count = int(existing.get("consecutiveFailures") or 0) + 1
            transaction.set(
                self.health_ref,
                {
                    "lastErrorAt": now,
                    "lastErrorClass": failure_class,
                    "lastErrorMessage": message,
                    "consecutiveFailures": count,
                    "collectorVersion": collector_version,
                },
                merge=True,
            )
            return count

        return self._run_transaction(_txn)

    # --- lease (design D14) -------------------------------------------------

    def acquire_lease(
        self, *, now: datetime, holder: str, seconds: int = DEFAULT_LEASE_SECONDS
    ) -> bool:
        """Take the advisory lease, or return False if another cycle holds it.

        Cloud Run Jobs do not guarantee non-overlap across invocations, so a
        cycle that overruns its interval could otherwise double up. A cycle that
        cannot take the lease exits without writing; the resulting gap is
        handled by coarse-interval behavior, which already has to work.
        """
        until = now + timedelta(seconds=seconds)

        def _txn(transaction) -> bool:
            snapshot = self.health_ref.get(transaction=transaction)
            existing = (snapshot.to_dict() or {}) if snapshot.exists else {}
            held_until = existing.get("leaseUntil")
            if held_until is not None and held_until > now:
                return False
            transaction.set(
                self.health_ref, {"leaseUntil": until, "leaseHolder": holder}, merge=True
            )
            return True

        return self._run_transaction(_txn)

    def release_lease(self, holder: str) -> None:
        def _txn(transaction) -> None:
            snapshot = self.health_ref.get(transaction=transaction)
            existing = (snapshot.to_dict() or {}) if snapshot.exists else {}
            if existing.get("leaseHolder") == holder:
                transaction.set(self.health_ref, {"leaseUntil": None}, merge=True)

        self._run_transaction(_txn)

    # --- reconciliation (design D11) ---------------------------------------

    def enqueue_reconciliation(
        self, *, sample_id: str, previous_local_date: str, now: datetime
    ) -> None:
        """Remember a sample awaiting a finalized previous-day total."""

        def _txn(transaction) -> None:
            snapshot = self.reconciliation_ref.get(transaction=transaction)
            pending = dict((snapshot.to_dict() or {}).get("pending") or {})
            pending.setdefault(
                sample_id, {"previousLocalDate": previous_local_date, "enqueuedAt": now}
            )
            # Not a merge: merging a map can add keys but never remove one, so
            # the queue is rewritten whole inside the transaction that read it.
            transaction.set(self.reconciliation_ref, {"pending": pending})

        self._run_transaction(_txn)

    def pending_reconciliations(self) -> dict[str, dict[str, Any]]:
        snapshot = self.reconciliation_ref.get()
        if not snapshot.exists:
            return {}
        return dict((snapshot.to_dict() or {}).get("pending") or {})

    def dequeue_reconciliation(self, sample_id: str) -> None:
        def _txn(transaction) -> None:
            snapshot = self.reconciliation_ref.get(transaction=transaction)
            pending = dict((snapshot.to_dict() or {}).get("pending") or {})
            pending.pop(sample_id, None)
            transaction.set(self.reconciliation_ref, {"pending": pending})

        self._run_transaction(_txn)

    def expired_reconciliations(
        self, now: datetime, window: timedelta = RECONCILIATION_WINDOW
    ) -> list[str]:
        """Sample IDs whose reconciliation window has closed.

        Bounded on purpose: an unbounded queue turns a transient LG delay into a
        permanent daily API cost. A permanently unresolved sample is honest; a
        fabricated one is not.
        """
        expired = []
        for sample_id, entry in self.pending_reconciliations().items():
            enqueued_at = entry.get("enqueuedAt")
            if enqueued_at is not None and now - enqueued_at >= window:
                expired.append(sample_id)
        return sorted(expired)

    def apply_reconciliation(
        self,
        *,
        sample_id: str,
        interval_value: Decimal,
        final_previous_day_total: Decimal,
        now: datetime,
    ) -> bool:
        """Fill in a deferred interval without touching a single raw value.

        Only derived fields and quality move. `raw.energy`, `raw.state`, and
        `energy.rawDailyTotal` are exactly what the API returned at the time and
        stay that way.
        """
        ref = self.observation_ref(sample_id)

        def _txn(transaction) -> bool:
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return False
            existing = snapshot.to_dict() or {}
            flags = list((existing.get("quality") or {}).get("flags") or [])
            if str(QualityFlag.RECONCILED) not in flags:
                flags.append(str(QualityFlag.RECONCILED))

            update: dict[str, Any] = {
                "quality.intervalStatus": str(IntervalStatus.DAY_ROLLOVER_RESOLVED),
                "quality.flags": flags,
                "reconciledAt": now,
            }
            for key, value in decimal_fields("intervalValue", interval_value).items():
                update[f"energy.{key}"] = value
            for key, value in decimal_fields(
                "finalPreviousDayTotal", final_previous_day_total
            ).items():
                update[f"energy.{key}"] = value

            transaction.update(ref, update)
            return True

        return self._run_transaction(_txn)


def usable_previous_readings(documents: Iterable[dict[str, Any]]) -> list[PreviousReading]:
    """Filter stored documents down to those usable as an energy baseline."""
    readings = []
    for document in documents:
        reading = TelemetryStore._as_previous_reading(document)
        if reading is not None:
            readings.append(reading)
    return readings
