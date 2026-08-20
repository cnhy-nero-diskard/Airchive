## Why

Historical telemetry cannot be reconstructed after the fact. Every day without a collector is a permanently missing day of LG air-conditioner history, so the cost of delay is irreversible data loss rather than deferred work.

A grounded read of LG's official `thinqconnect` SDK (v1.0.13) confirms the AC state resource exposes **no instantaneous power or wattage property** — only `operation`, `temperatureInUnits`, `powerSave`, `airFlow`, `airQualitySensor`, `filterInfo`, `windDirection`, and timer groups. The sole energy signal available through the official API is the `DAILY` energy-usage counter. Sampling that cumulative counter on a short cadence is therefore not one option among several; it is the only way to obtain sub-daily energy resolution from a supported, non-reverse-engineered source. That makes starting collection now both urgent and architecturally unavoidable.

## What Changes

- Introduce a standalone, headless telemetry collector — no UI, no dashboard, no analytics layer, independent of Backlogium or any other application.
- Integrate the **official** LG ThinQ Connect API via LG's `thinqconnect` Python SDK, using a Personal Access Token and a **stable, persisted** client ID. No MITM/proxy interception, no reverse-engineered clients, no unofficial auth flows.
- Sample, roughly every 5 minutes, the device's cumulative `DAILY` energy total plus its full readable AC state, and persist each observation as an immutable historical record in a dedicated Firestore project.
- Preserve the raw source value authoritatively. Derived values (interval consumption, elapsed seconds) are recomputable from raw records, never the only thing stored.
- Classify every observation with explicit data quality rather than silently emitting wrong numbers: first-observation baselines, missed polls, day rollover, anomalous decreases, partial API success, device-offline, and rate limiting are all distinct, first-class outcomes.
- Reconstruct cross-midnight intervals using LG's finalized previous-day total, and defer (not fabricate) the interval when that total is not yet available.
- Persist observations under deterministic, slot-derived document IDs so retries reconcile a sample instead of duplicating it.
- Maintain a small mutable collector-health record and a set of inspection commands so the system can be checked without building a UI.
- Establish and validate the Firebase/Firestore environment, credentials, and debugging workflow **before** the poller is implemented.

### Gated discovery phase

Three assumptions are load-bearing and currently unverified against the user's actual hardware. They are resolved in a discovery phase that **gates** schema finalization and implementation:

1. **The `DAILY` counter must advance intraday.** If LG only updates it once per day, high-resolution energy telemetry is impossible via the official API and the collector's value narrows to state-only history. Everything downstream depends on this.
2. **The energy property name and units must be read from the device's energy profile,** not assumed to be `energyConsumption`/kWh.
3. **LG's day boundary must be determined empirically.** The daily bucket resets on a timezone owned by LG/the device, which may not match the collector's configured timezone. A wrong boundary silently corrupts day-rollover handling.

Discovery is read-only. No AC control commands are issued.

## Capabilities

### New Capabilities

- `thinq-connect-integration`: Authenticated, rate-limit-aware access to the official ThinQ Connect API — configuration inputs, stable client identity, one-time device/profile discovery with caching, a failure taxonomy derived from ThinQ error codes, and credential hygiene at the SDK boundary.
- `energy-observation`: The measurement semantics — raw cumulative daily energy, interval delta calculation, elapsed-time handling, cross-midnight reconciliation, anomaly classification, and the data-quality model.
- `telemetry-persistence`: The Firestore data model — immutable observation series, deterministic sample IDs, idempotent write and precedence rules, cached profile/metadata records, mutable collector health, and intentional indexing policy.
- `collector-runtime`: The execution shape — poll cadence, startup configuration validation, restart/state recovery from Firestore, bounded retries and backoff, structured logging with correlation IDs, graceful degradation, and deployment topology.
- `collector-operations`: Operator-facing capability without a UI — Firebase environment setup and connectivity validation, discovery/probing commands, and telemetry inspection commands.

### Modified Capabilities

None. This is a greenfield repository with no existing specs.

## Impact

**New dependencies**
- `thinqconnect` (official LG SDK, Apache-2.0, requires Python ≥3.10). Note it transitively pulls `aiohttp`, `awsiotsdk`, and `pyOpenSSL` — the latter two serve MQTT event subscription, which this change explicitly does not use, so they are dead weight in the runtime image.
- `google-cloud-firestore` (server-side Admin access).

**New external systems**
- A dedicated Firebase project (suggested `lg-ac-telemetry`) with a Cloud Firestore Standard database. **The database region is permanent once created.**
- A Google Cloud runtime for ~5-minute scheduled execution, plus Secret Manager for the ThinQ PAT.

**Security posture**
- The collector is server-side only. Firestore client security rules are locked closed; there is no frontend to serve. Access is via Admin/IAM.
- No service-account JSON key is shipped with the deployed application; deployment uses the runtime's attached service account via Application Default Credentials. Local development prefers ADC/gcloud credentials.
- **A concrete SDK hazard requires an explicit mitigation:** `ThinQAPIException` is constructed with the *request* headers, which include `Authorization: Bearer <PAT>`. Any logger that serializes the exception object or its attributes would leak the token. A sanitization boundary at the SDK edge is a hard requirement, not a nicety.

**Explicitly out of scope**
- Dashboards, charts, React/frontend applications, analytics UI, user-facing authentication.
- ThinQ MQTT/event subscription (documented as a future enhancement only).
- AC control commands.
- Any revival of prior mitmproxy/interception work.

**Cost**
- ~288 observations/day (~105k/year) for one device. Write volume is negligible against Firestore free-tier limits; long-term *storage* of raw payloads, not write rate, is the eventual cost driver.
