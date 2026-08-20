## Context

See `proposal.md` — Why. This section records only the constraints that shape the approach.

The repository is greenfield: one commit, OpenSpec scaffolding only, no application code, no language toolchain chosen. Every decision below is therefore unconstrained by existing convention, and the design should stay small enough that a later analytics layer is not boxed in.

The binding constraints come from LG's official SDK. The following were established by reading `thinqconnect` 1.0.13 directly, not inferred:

| Observation | Consequence |
|---|---|
| `ThinQApi` exposes exactly `async_get_device_list`, `async_get_device_profile`, `async_get_device_status`, `async_get_device_energy_profile`, `async_get_device_energy_usage` | The API surface assumed by the proposal exists as described. No reverse engineering needed. |
| AC state resources are `operation`, `temperatureInUnits`, `twoSetTemperature`, `timer`, `sleepTimer`, `powerSave`, `airFlow`, `airQualitySensor`, `filterInfo`, `display`, `windDirection`, `airConJobMode`. There is **no** instantaneous power/wattage property. | The `DAILY` counter is the only energy signal. Sub-daily energy resolution stands or falls on it. |
| `async_request` returns `payload["response"]` on success, else raises `ThinQAPIException`. The `ClientResponse` is discarded. | HTTP status and `Retry-After` are unavailable. Rate limiting must be detected from ThinQ error codes. |
| `ThinQAPIException(code, message, headers)` is constructed with `self._generate_headers(...)` — the **outbound request** headers, containing `Authorization: Bearer <PAT>`. | Serializing the exception leaks the token. A sanitizing boundary is mandatory. |
| `await response.json()` is called before the `response.ok` check | A non-JSON error body (gateway HTML, empty 502/504) raises `aiohttp.ContentTypeError`, not `ThinQAPIException`. |
| `ConnectBaseDevice.get_daily_energy_usage()` calls `_check_date_format`, which compares against `date.today()` — the **process's system-local date** — and raises `ValueError` when `today < end_date`. | A UTC-clocked container requesting the device's local "today" raises for the first 8 hours of each Manila day, precisely when rollover reconciliation runs. |
| `energy_profile["result"]["property"]` is the supported-property list | Startup validation of the configured energy property is cheap and exact. |
| `thinqconnect/__init__.py` line 39 eagerly imports `mqtt_client`, pulling `awsiotsdk` and `pyOpenSSL` | ~430 ms and ~400 modules of unavoidable import cost even though MVP uses no MQTT. Importing a submodule does not avoid it. |
| `requires_python >= 3.10`; `get_region_from_country("PH")` → `KIC` | Python 3.12+ is fine; the Philippines routes to `api-kic.lgthinq.com`. |

## Goals / Non-Goals

**Goals**

- Never store a derived number that cannot be re-derived from raw values in the same dataset.
- Make every failure mode produce a *classified* record or no record — never a plausible-looking wrong number.
- Keep the deployed footprint small enough that running it for years is uninteresting, operationally and financially.
- Make the system inspectable from a terminal by a human or a future agent with no prior context.

**Non-Goals (design level)**

- No abstraction layer over Firestore or over ThinQ "in case we swap them later." One provider each, directly.
- No multi-device fan-out machinery. The data model is keyed per device, but the runtime targets one configured device.
- No aggregation, rollup, or materialized view. Raw series only.
- No backfill of history predating the collector. It does not exist and cannot be created.

## Decisions

### D1 — Python 3.12, official SDK, low-level `ThinQApi` only

Python is effectively forced: `thinqconnect` is LG's official SDK and is Python-only. Choosing anything else means abandoning the "official SDK" requirement.

Within the SDK, use `ThinQApi` (low-level) and **not** `ConnectBaseDevice`/`AirConditionerDevice`.

*Why:* the device wrapper's `_check_date_format` couples energy requests to the process's system-local date via `date.today()`. Requesting the device's local "today" from a UTC-clocked container raises `ValueError` for the first 8 hours of every Manila day. Setting the container `TZ` would mask it, but that makes correctness depend on an environment variable that no test would catch and that silently breaks if the deployment region or base image changes. The low-level call performs no date validation. We do our own property validation at startup against `energy_profile["result"]["property"]`.

*Alternatives:* use the wrapper and pin container `TZ` (rejected — invisible coupling, breaks under UTC default); wrap the endpoint by hand with `aiohttp` (rejected — reimplements auth headers, region routing, and message IDs for no gain).

*Trade-off:* we forgo the wrapper's typed property model and must map the raw state payload ourselves. Acceptable, because we want the raw payload preserved verbatim anyway.

### D2 — A sanitizing boundary is the only code allowed to touch SDK exceptions

All SDK calls pass through one narrow module. It catches `ThinQAPIException`, `aiohttp` errors, and `asyncio.TimeoutError`, and converts each into an internal `ThinqFailure(failure_class, code, error_name, safe_message)`. The original exception object never escapes this boundary — not to a logger, not to a handler, not into a Firestore document.

*Why:* `ThinQAPIException.headers` contains the bearer token. `logger.exception(...)`, a structured logger that walks `__dict__`, or an error-reporting integration would each exfiltrate the PAT into logs that are retained and broadly readable. This is not a hypothetical: it is the default behavior of ordinary logging code against this specific exception type.

*Reinforcement:* a unit test asserts the PAT string appears in no rendered log record or serialized failure, using a sentinel token value.

### D3 — Failure classification keys on ThinQ error codes, not HTTP status

HTTP status is not observable through the SDK, so classification is code-driven:

| Class | Trigger | Response |
|---|---|---|
| `RATE_LIMITED` | `1306 EXCEEDED_API_CALLS`, `1305`/`1309 NOT_ALLOWED_API` | Exponential backoff + jitter; flag the sample; reduce effective rate |
| `AUTH_FATAL` | `1103`/`1218 INVALID_TOKEN`, `1302 NOT_FOUND_TOKEN` | **No retry.** Surface loudly in health; operator must replace the PAT |
| `DEVICE_OFFLINE` | `1222 NOT_CONNECTED_DEVICE` | Not a collector fault. Record as device unavailability |
| `TRANSIENT` | `2000 INTERNAL_SERVER_ERROR`, `2209 DEVICE_RESPONSE_DELAY`, `2210 RETRY_REQUEST`, `2212 SYNCING` | Bounded retry within the cycle |
| `CONFIG_FATAL` | `1219`/`1220`/`1221` not-supported, `1205`/`1224` bad device | No retry; fail startup validation loudly |
| `MALFORMED` | `aiohttp.ContentTypeError`, `JSONDecodeError`, missing expected keys | Record and continue; never crash |
| `TRANSPORT` | `aiohttp.ClientError`, timeout | Bounded retry |

*Why the auth/transient split matters:* retrying a revoked PAT with backoff produces an infinitely quiet failure. Treating it as fatal converts silent data loss into a visible operator signal.

*Alternative rejected:* bypass the SDK to read HTTP status and `Retry-After`. Costs the official-SDK guarantee for a marginal backoff refinement, since ThinQ signals the same condition in-band via `1306`.

### D4 — Energy arithmetic in `Decimal`, never binary float

Readings are decimal strings at fixed precision. `8.751 - 8.732` in IEEE-754 yields `0.019000000000000794`. Parse readings with `Decimal(str(value))`, subtract in `Decimal`, and quantize the result to the precision established during validation.

*Why:* stored values are the permanent record. Float noise in `intervalKWh` is indistinguishable from real measurement noise once the raw source values have scrolled out of anyone's memory, and it poisons any future sum or comparison.

### D5 — Quality is three orthogonal fields, not one enum

```
quality: {
  intervalStatus: <exactly one>     # why intervalKWh is what it is
  flags: [<zero or more>]           # independent coexisting conditions
}
source: {
  energy: { ok, failureClass, errorCode },
  state:  { ok, failureClass, errorCode }
}
```

`intervalStatus` ∈ `NORMAL | NEW_BASELINE | MISSING_PREVIOUS_SAMPLE | COARSE_INTERVAL | DAY_ROLLOVER_RESOLVED | DAY_ROLLOVER_UNRESOLVED | ANOMALOUS_DECREASE | MULTI_DAY_GAP | ENERGY_UNAVAILABLE`

`flags` ⊆ `RATE_LIMITED | DEVICE_OFFLINE | UNCHANGED_COUNTER | PARTIAL_OBSERVATION | RECONCILED | IMPLAUSIBLE_FINAL_TOTAL`

*Why:* the proposal's requirement not to overload one field is load-bearing. `RATE_LIMITED` and `COARSE_INTERVAL` are simultaneously true and neither implies the other; `ENERGY_API_FAILED` is a property of a *source*, not of the interval. Flags are an array so Firestore `array-contains` makes each independently queryable — a single enum would force either a combinatorial explosion of names or lossy precedence.

*Note:* `NEW_BASELINE` (no prior observation has ever existed) is kept distinct from `MISSING_PREVIOUS_SAMPLE` (a prior observation exists but carries no usable energy value). They look alike and imply different things about collector health.

### D6 — Sample IDs are UTC-floored slot stamps

`sampleId = strftime("%Y%m%dT%H%M%SZ")` of the scheduled instant floored to the poll interval, in UTC. The proposal's example holds: `20260820T091500Z` is 17:15 Asia/Manila.

*Why UTC:* the ID is unambiguous across DST and any future timezone reconfiguration, and it sorts lexicographically in the same order as chronologically — so "latest sample" is a descending key scan needing no index. A local-time ID would collide or reorder under a DST transition.

Both `scheduledAt` (the slot) and `observedAt` (when the reading was actually taken) are stored. Deltas use `observedAt`; identity uses the slot.

### D7 — Idempotency by transactional completeness precedence

Write inside a Firestore transaction. Compute `completeness = (2 if energy.ok else 0) + (1 if state.ok else 0)`, then:

- document absent → create
- `new.completeness > existing.completeness` → overwrite (an upgrade)
- `new.completeness == existing.completeness` → no-op (first writer wins; avoids `observedAt` churn on a duplicate)
- `new.completeness < existing.completeness` → skip, log the refusal
- reconciliation-only updates → patch derived fields and add `RECONCILED`; never touch stored raw values

*Why:* deterministic IDs alone prevent duplicate *creation* but introduce a stale-overwrite hazard — a delayed retry for slot T could clobber a better record written since. Precedence closes that hole and makes replay safe, which matters because a scheduled runtime will occasionally double-deliver.

### D8 — Firestore layout

```
devices/{deviceId}                     device identity, alias, model, current metadata pointer
  telemetry/{sampleId}                 immutable observation series  ← source of truth
  dailyTotals/{localDate}              LG's finalized per-day totals (cached, independently useful)
  metadata/{profileVersion}            versioned profile + energy-profile snapshots
  metadata/current                     pointer to the active profile version
  runtime/collector                    mutable health (overwritten in place)
  runtime/reconciliation               bounded queue of sampleIds awaiting a finalized total
```

`dailyTotals` is deliberate rather than incidental: the finalized previous-day total is fetched once, reused by rollover reconciliation instead of being re-requested per cycle, and is itself a clean historical ledger to reconcile the 5-minute series against later. It also gives the once-per-day extra call a natural cache boundary satisfying the per-cycle call budget.

### D9 — Raw payloads retained, excluded from indexing

Each observation carries `raw.energy` and `raw.state` — the SDK's returned `response` object verbatim, minus nothing (it contains no credentials; the envelope the SDK strips carries none either). A single-field index exemption is configured on `raw`, with array and map descent disabled.

*Why:* Firestore auto-indexes every scalar including nested map subfields. At ~105k documents/year against deeply nested state payloads, index storage would exceed document storage and grow with LG's payload shape rather than with our query needs. Exempting `raw` keeps it human-readable in the console — which matters for the no-UI inspection workflow — while removing it from index cost.

*Alternative:* store `raw` as a JSON string. Equally cheap on indexing but unreadable in the console and awkward to spot-check. Kept as a fallback if exemption granularity disappoints.

*Sizing:* ~2–4 KB/document → roughly 300–450 MB/year. Firestore's 1 GiB free tier covers about two years; storage, not write volume, is the eventual cost driver. Export to GCS/BigQuery is a future option, and raw history is never deleted in place.

### D10 — Cloud Run Job triggered by Cloud Scheduler

| | Scheduled job | Always-on service | Local process |
|---|---|---|---|
| Cost at 5 min | Within free tier | Pays 24/7 to idle | Free, plus a machine that must never sleep |
| Restart behavior | Every run is a cold start; no recovery path to get wrong | Needs supervision + liveness | Fails silently when the machine reboots |
| Credentials | Attached service account, no key file | Same | Tempts a long-lived key on disk |
| State | Already in Firestore, so statelessness costs nothing | In-memory state becomes a liability | Same |
| Failure blast radius | One invocation | Whole process | Whole process |

Chosen: **Cloud Run Job + Cloud Scheduler**, one invocation per slot, PAT from Secret Manager, Firestore via the attached service account's ADC. No public HTTP surface.

*Why:* restart recovery (D11/spec) is required regardless, which erases the only real advantage an always-on process has — in-memory continuity. Once every invocation must reconstruct state from Firestore anyway, paying for a 24/7 process buys nothing. The proposal's instruction not to adopt an always-on server merely because polling exists is satisfied on the merits, not by preference.

*Cost check:* 8,640 executions/month × ~5 s ≈ 43k vCPU-s against a 180k free tier; ~9k Firestore writes/month against 20k/day free. Effectively zero.

*Accepted cost:* ~288 cold starts/day, each paying the unavoidable ~430 ms `thinqconnect` import (D-context) plus interpreter start — call it 2–4 s per run. Irrelevant at a 5-minute cadence. Mitigation is a slim base image, not architecture.

### D11 — Day boundary is configuration, validated empirically; rollover reconciliation is bounded

`LG_DAY_TIMEZONE` (default `Asia/Manila`) determines `localDate` and rollover detection. Discovery must confirm it by observing when the counter actually resets, because the bucket boundary belongs to LG, not to us.

On the first cycle of a new local day: fetch the previous day's finalized total once, cache it in `dailyTotals/{localDate}`, and reconstruct

```
crossDayDelta = (finalPreviousDayKWh − previousDailyKWh) + currentDailyKWh
```

guarded by `finalPreviousDayKWh >= previousDailyKWh`. If the guard fails, flag `IMPLAUSIBLE_FINAL_TOTAL` and leave the interval null. If the total is unavailable, mark `DAY_ROLLOVER_UNRESOLVED` and enqueue the sampleId in `runtime/reconciliation`. Retry on subsequent cycles until resolved or a 24-hour window elapses, then leave it permanently unresolved.

*Why bounded:* an unbounded reconciliation queue turns a transient LG delay into a permanent daily API cost and an ever-growing document. A permanently unresolved sample is honest; a fabricated one is not.

### D12 — Previous-reading lookup is a bounded descending scan

Query `telemetry` ordered by document ID descending, limit ~12, and take the newest document with a usable raw energy value. Document-ID ordering is chronological by D6, so no composite index is required.

*Why bounded:* an unbounded "find any previous reading" walk after a long outage could scan far and would produce a meaninglessly wide interval anyway. If no usable baseline exists within the window, proceed as `MISSING_PREVIOUS_SAMPLE` — correct and cheap.

### D13 — Credentials

- **Local:** ADC via `gcloud auth application-default login`. No key file. `GOOGLE_APPLICATION_CREDENTIALS` is honored only as an escape hatch, documented with revocation steps, gitignored, stored outside the repo.
- **Deployed:** attached service account, `roles/datastore.user` only. No key material in the image.
- **PAT:** Secret Manager, injected at runtime. Never in the image, never in plain config, never logged (D2).
- **Firestore client rules:** deny all. There is no frontend; opening rules for a nonexistent client is pure attack surface.

### D14 — Overlap prevention

Cloud Run Jobs do not guarantee non-overlap across invocations, so a cycle that overruns 5 minutes could double up. A conditional create on `runtime/collector.leaseUntil` acts as a short advisory lease; a cycle that cannot take the lease exits without writing. Any resulting gap is handled by `COARSE_INTERVAL`, which is already required behavior.

### D15 — Discovery and inspection as CLI subcommands

One entry point with subcommands: `discover`, `validate-counter`, `check-firestore`, `poll --once`, `latest`, `health`, `anomalies`, `compare`. Read-only except `poll`.

*Why a CLI rather than docs describing console clicks:* the operations spec requires terminal-answerable questions, and a future agent inheriting this repo needs executable affordances, not prose. `check-firestore` exists specifically so the connectivity gate in the operations spec is a command that passes or fails, not a manual ritual.

## Risks / Trade-offs

- **The `DAILY` counter may not advance intraday** → This invalidates the project's core premise, so it is validated *first*, in a gated discovery phase, before any collector code is written. If it fails, the change is revisited; state-only telemetry may still proceed. This is the single largest risk and the reason for the phase ordering.
- **LG's day boundary may differ from the configured timezone** → Silently corrupts rollover handling. Mitigated by empirical confirmation in discovery (D11) and by reporting a mismatch as a finding rather than proceeding on assumption.
- **The PAT can leak through ordinary logging of SDK exceptions** → Single sanitizing boundary (D2) plus a sentinel-token test asserting the secret never reaches rendered output.
- **The SDK's `date.today()` coupling causes a daily 8-hour outage under a UTC clock** → Bypassed entirely by using low-level `ThinQApi` (D1). A test exercises the energy path with the process clock set to UTC while requesting a Manila-local date.
- **PAT expiry silently ends collection** → Classified `AUTH_FATAL`, never retried, recorded distinctly in health. Alerting on `consecutiveFailures` and on the fatal class is left to deployment configuration.
- **Deterministic IDs enable stale overwrites** → Transactional completeness precedence (D7).
- **Raw payload retention drives long-term storage cost** → Index exemption (D9) plus documented growth projection; archival deferred, deletion of raw history excluded.
- **`awsiotsdk`/`pyOpenSSL` are dead weight** → ~430 ms import and a fatter image for MQTT we do not use. Unavoidable while using the official SDK; accepted, and a reason to prefer a slim base image.
- **Cold starts on every invocation** → Accepted at this cadence; the alternative costs 24/7 compute to avoid a few seconds.
- **Firestore region is permanent** → Called out explicitly in the setup guidance before the database is created, because it cannot be undone later.
- **Single-device scope** → The data model is device-keyed so a second device is additive, but the runtime targets one. Multi-device orchestration is deliberately unbuilt.

## Migration Plan

Greenfield; nothing to migrate. Deployment order matters because each step gates the next:

1. Create the Firebase project and Firestore database (region choice is permanent). Lock client rules to deny-all.
2. `check-firestore` — write/read/delete round trip from local ADC. Confirm in the console. **Gate: must pass before collector logic is built.**
3. Configure the PAT, country code, and a once-generated client ID.
4. `discover` — identify the AC, capture profiles, confirm the energy property, unit, and precision.
5. `validate-counter` — sample over hours of normal use. **Gate: confirms or refutes the intraday-advance premise and the day boundary.**
6. Record findings; finalize the provisional schema against what the device actually exposes.
7. Implement the collector against the now-validated assumptions.
8. `poll --once` locally, verify in the console.
9. Deploy the Cloud Run Job with the attached service account and Secret Manager; schedule at 5 minutes.
10. Confirm via `latest` and `health` that scheduled runs land.

**Rollback:** pause the Cloud Scheduler job. Collected telemetry is append-only and unaffected; the next run resumes from Firestore state with a `COARSE_INTERVAL` gap. There is no destructive step to reverse.

## Open Questions

These are deferrable: each is answered by the discovery phase without changing the specs, the approach, or the task breakdown.

- The exact energy property name, its unit, and its decimal precision — read from the device's energy profile, not assumed to be `energyConsumption`/kWh.
- Observed update latency of the current-day counter, and whether LG serves cached or retroactively revised values. Affects only the documented interpretation of `UNCHANGED_COUNTER`, not the schema.
- Whether this AC model exposes `powerSaveEnabled` as a boolean only, or some percentage-style energy-control value. The proposal's illustrative `energyControl: 40` does not appear in the SDK's AC property vocabulary and may not exist on this device.
- Which of the twelve AC state resource groups this specific model populates.
- Whether ThinQ's published per-token call quota leaves headroom above 576 calls/day for one device, and whether that would constrain a future second device.
