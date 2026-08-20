# Airchive — operations

Everything needed to run, understand, and inspect the collector. If you are
setting it up for the first time, start with [setup.md](setup.md) and come back
here.

- [What this is](#what-this-is)
- [Configuration](#configuration)
- [Running it](#running-it)
- [Deployment](#deployment)
- [The stored data model](#the-stored-data-model)
- [Interval and delta semantics](#interval-and-delta-semantics)
- [Day rollover and reconciliation](#day-rollover-and-reconciliation)
- [The quality model](#the-quality-model)
- [Idempotency](#idempotency)
- [Inspection commands](#inspection-commands)
- [Collector health](#collector-health)
- [Rate limiting](#rate-limiting)
- [Credential hygiene](#credential-hygiene)
- [Known device and API limitations](#known-device-and-api-limitations)
- [Storage growth](#storage-growth)
- [Future enhancements](#future-enhancements)
- [Testing](#testing)

---

## What this is

A headless collector for one LG air conditioner. Every five minutes it reads two
things through LG's official ThinQ Connect API — the device's cumulative
current-day energy counter and its full readable state — and writes one
immutable observation to Firestore.

There is no UI, no dashboard, and no analytics layer, and it depends on no other
application. Inspection happens from a terminal.

**Why sample a daily counter instead of reading power?** The official API exposes
no instantaneous power or wattage property for air conditioners. The state
resource carries `operation`, `temperatureInUnits`, `powerSave`, `airFlow`,
`airQualitySensor`, `filterInfo`, `windDirection`, and timer groups — and no
watts. The `DAILY` energy counter is the only energy signal available, so
sub-daily energy resolution means sampling it often and differencing
consecutive readings. That is the whole design in one sentence, and everything
below follows from it.

---

## Configuration

Every variable, all read from the environment (a local `.env` is loaded if
present and never overrides real environment variables).

| Variable | Required | Default | What it does |
|---|---|---|---|
| `LG_THINQ_PAT` | yes | — | ThinQ Personal Access Token. From Secret Manager when deployed. Never logged, never stored. |
| `LG_COUNTRY_CODE` | yes | — | Two-letter ISO 3166-1 code of the **LG account**. Selects the API region; `PH` routes to `KIC`. |
| `LG_CLIENT_ID` | yes | — | Generated **once** and reused forever. Startup fails rather than inventing one. |
| `LG_DEVICE_ID` | yes | — | Target device, from `airchive discover`. |
| `LG_ENERGY_PROPERTY` | yes | — | Energy property to poll. Must appear in the device's energy profile. |
| `FIREBASE_PROJECT_ID` | yes | — | Project holding the telemetry database. |
| `POLL_INTERVAL_SECONDS` | no | `300` | Sampling cadence. Interval classification always uses *actual* observation times, never this. |
| `LG_DAY_TIMEZONE` | no | `Asia/Manila` | Timezone that defines the local day and the rollover boundary. Confirm it empirically — see [setup.md](setup.md) step 7. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL`. |
| `GOOGLE_APPLICATION_CREDENTIALS` | no | unset | **Local escape hatch only.** Prefer ADC. Never set in the deployed job. |

Startup validation runs before any network call or write, reports **every**
offending value at once, and never echoes a secret:

```
$ airchive poll --once
Invalid configuration:
  - LG_THINQ_PAT is required but missing or empty. Obtain it from the LG ThinQ developer portal.
  - LG_CLIENT_ID is required but missing or empty. Generate one once (...) and persist it; ...
```

---

## Running it

```bash
airchive --help              # every subcommand
airchive check-firestore     # storage round trip (Gate A)
airchive discover            # devices, profiles, energy property, unit, precision
airchive validate-counter    # does the daily counter advance intraday? (Gate B)
airchive poll --once         # one cycle, then exit  <- the deployed shape
airchive poll                # a cycle per interval until interrupted
airchive latest --limit 20   # recent observations
airchive health              # collector health record
airchive anomalies --since 2026-08-20T00:00:00Z
airchive compare             # stored vs a fresh live reading
```

Everything except `poll` is read-only. `compare` in particular writes nothing to
the telemetry series. No command ever issues a device **control** command.

`poll --once` is the shape the deployment uses: one cycle per invocation, all
state reconstructed from Firestore. `poll` without `--once` runs a cycle per
interval and stops cleanly on SIGINT/SIGTERM, finishing or abandoning the cycle
in flight so no partial observation is left behind.

---

## Deployment

### Why a scheduled job rather than a long-running service

| | **Cloud Run Job + Scheduler** | Always-on service | Local process |
|---|---|---|---|
| Cost at 5 min | within free tier | pays 24/7 to idle | free, plus a machine that must never sleep |
| Restart behavior | every run is a cold start; no recovery path to get wrong | needs supervision and liveness | fails silently when the machine reboots |
| Credentials | attached service account, no key file | same | tempts a long-lived key on disk |
| State | already in Firestore, so statelessness costs nothing | in-memory state becomes a liability | same |
| Failure blast radius | one invocation | whole process | whole process |
| Observability | one log stream per execution | needs its own | local only |
| Rate limits | same 2 calls per slot either way | same | same |

**Chosen: Cloud Run Job + Cloud Scheduler.** Restart recovery is required
regardless — every cycle reconstructs its baseline from Firestore — which
erases the only real advantage an always-on process has, in-memory continuity.
Once that is gone, paying for a 24/7 process buys nothing. Polling alone is not
a reason to adopt a server.

*Cost check:* 8,640 executions/month × ~5 s ≈ 43k vCPU-seconds against a 180k
free tier; ~9k Firestore writes/month against 20k/day free.

*Accepted cost:* ~288 cold starts a day, each paying the unavoidable ~430 ms
`thinqconnect` import plus interpreter start — 2–4 s per run, irrelevant at this
cadence. The mitigation is a slim base image, not a different architecture.

### Steps

```bash
PROJECT=lg-ac-telemetry
REGION=asia-southeast1

# 1. Build and push the image.
gcloud builds submit --tag gcr.io/$PROJECT/airchive:latest

# 2. A service account with exactly one role.
gcloud iam service-accounts create airchive-collector \
    --display-name="Airchive telemetry collector"
gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:airchive-collector@$PROJECT.iam.gserviceaccount.com" \
    --role="roles/datastore.user"

# 3. The PAT lives in Secret Manager, never in the image.
printf '%s' "$LG_THINQ_PAT" | gcloud secrets create lg-thinq-pat --data-file=-
gcloud secrets add-iam-policy-binding lg-thinq-pat \
    --member="serviceAccount:airchive-collector@$PROJECT.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"

# 4. The job. No key file: credentials come from the attached identity.
gcloud run jobs create airchive-poll \
    --image=gcr.io/$PROJECT/airchive:latest \
    --region=$REGION \
    --service-account=airchive-collector@$PROJECT.iam.gserviceaccount.com \
    --set-secrets=LG_THINQ_PAT=lg-thinq-pat:latest \
    --set-env-vars=FIREBASE_PROJECT_ID=$PROJECT,LG_COUNTRY_CODE=PH,LG_CLIENT_ID=...,LG_DEVICE_ID=...,LG_ENERGY_PROPERTY=...,LG_DAY_TIMEZONE=Asia/Manila \
    --max-retries=1 \
    --task-timeout=300s

gcloud run jobs execute airchive-poll --region=$REGION --wait

# 5. Every five minutes.
gcloud scheduler jobs create http airchive-poll-schedule \
    --location=$REGION \
    --schedule="*/5 * * * *" \
    --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/airchive-poll:run" \
    --http-method=POST \
    --oauth-service-account-email=airchive-collector@$PROJECT.iam.gserviceaccount.com
```

`--max-retries=1` is deliberate: a retried invocation is safe (writes are
idempotent by slot with completeness precedence) but rarely useful, since the
next slot is only five minutes away.

### Verifying a deployment

1. `gcloud run jobs execute airchive-poll --region=$REGION --wait` → then
   `airchive latest --limit 1` shows the new observation.
2. Let three scheduled executions run → `airchive latest --limit 3` shows three
   distinct sequential sample IDs, five minutes apart.
3. In Cloud Logging, filter on the `sampleId` from a stored observation; its
   cycle logs come back. That correlation is the whole point of the identifier.

**Rollback** is pausing the Scheduler job. Collected telemetry is append-only and
unaffected; the next run resumes from Firestore state and marks the gap
`COARSE_INTERVAL`. There is no destructive step to reverse.

---

## The stored data model

```
devices/{deviceId}                     identity, alias, model, metadata pointer
  telemetry/{sampleId}                 immutable observation series  <- source of truth
  dailyTotals/{localDate}              LG's finalized per-day totals (cached)
  metadata/{profileVersion}            versioned profile + energy-profile snapshots
  metadata/current                     pointer to the active profile version
  runtime/collector                    mutable health, overwritten in place
  runtime/reconciliation               bounded queue awaiting a finalized total
```

`sampleId` is the scheduled slot floored to the interval and stamped in UTC:
`20260820T091500Z` is the 17:15 Asia/Manila slot. UTC because the identifier must
stay unambiguous across DST and any later timezone change, and because it then
sorts lexicographically in the same order as chronologically.

One observation:

```json
{
  "sampleId": "20260820T091500Z",
  "deviceId": "...",
  "scheduledAt": "<timestamp>",   "observedAt": "<timestamp>",  "persistedAt": "<timestamp>",
  "localDate": "2026-08-20",      "timezone": "Asia/Manila",    "completeness": 3,
  "energy": {
    "property": "energyConsumption", "unit": "kWh",
    "rawDailyTotal": "2.150",        "rawDailyTotalNumber": 2.15,
    "intervalValue": "0.050",        "intervalValueNumber": 0.05,
    "intervalSeconds": 300.0,
    "previous": { "sampleId": "...", "observedAt": "...", "localDate": "...",
                  "rawDailyTotal": "2.100", "rawDailyTotalNumber": 2.1 },
    "finalPreviousDayTotal": null,   "finalPreviousDayTotalNumber": null
  },
  "quality": { "intervalStatus": "NORMAL", "flags": [] },
  "source":  { "energy": {"ok": true, "failureClass": null, "errorCode": null, "errorName": null},
               "state":  {"ok": true, "failureClass": null, "errorCode": null, "errorName": null} },
  "state":   { "operation": {"airConOperationMode": "POWER_ON"}, "temperature": {"unit": "C"} },
  "raw":     { "energy": {...}, "state": {...} },
  "metadataVersion": "...", "collectorVersion": "0.1.0"
}
```

**Decimal values are stored as strings, with a `…Number` float mirror
alongside.** Firestore has no decimal type, and a float is a binary
approximation: `8.751 - 8.732` in IEEE-754 is `0.019000000000000794`. The string
is authoritative — every derived number is recomputable from the stored strings
— and the mirror exists purely so range queries and aggregations work without
parsing every document.

**`raw.energy` and `raw.state` are the API's responses verbatim**, so the dataset
can be reinterpreted later against a better understanding of it. They contain
response content only: no request headers, no token, no credential of any kind.
They are exempted from indexing (`firestore.indexes.json`) because Firestore
otherwise indexes every nested scalar, and index storage would grow with LG's
payload shape rather than with anything we query.

**Ordering** keys on the mirrored `sampleId` field rather than the document key,
because Firestore rejects descending key scans outright. The value is identical,
so the chronological-sort property still holds, and the automatic single-field
index covers it.

---

## Interval and delta semantics

Interval consumption is the difference between the current cumulative reading
and the previous **usable** one, in `Decimal` throughout, quantized to the
precision the device itself reports.

Duration comes from the two readings' actual `observedAt` timestamps — never
from `POLL_INTERVAL_SECONDS`. If cycles were missed, the duration reflects that:
readings at 12:00 and 12:15 of 2.100 and 2.190 give 0.090 over ~900 seconds,
marked `COARSE_INTERVAL`, not 0.090 over a nominal 300.

Rules, each one preventing a specific wrong number:

| Situation | Interval | Status |
|---|---|---|
| Same day, counter advanced | difference | `NORMAL` |
| Same day, counter unchanged | `0` | `NORMAL` + `UNCHANGED_COUNTER` flag |
| Same day, more than 1.5× the cadence elapsed | difference | `COARSE_INTERVAL` |
| Same day, counter **decreased** | `null` | `ANOMALOUS_DECREASE` |
| No prior observation has ever existed | `null` | `NEW_BASELINE` |
| A prior observation exists but has no usable energy | `null` | `MISSING_PREVIOUS_SAMPLE` |
| Previous reading is from yesterday | reconstructed — see below | `DAY_ROLLOVER_*` |
| Previous reading is older than yesterday | `null` | `MULTI_DAY_GAP` |
| The energy request failed | `null` | `ENERGY_UNAVAILABLE` |

A decrease within a day is a provider revision, not negative consumption. The
earlier observation is never rewritten; the new one is stored with the
classification, so the revision stays visible in the historical series.

An observation whose energy request failed is never used as the baseline for the
next interval. The lookup walks back up to 12 documents to find a usable one, and
if it finds none it proceeds as `MISSING_PREVIOUS_SAMPLE` rather than using an
unusable one.

---

## Day rollover and reconciliation

At LG's midnight the counter resets, so subtracting across it would give a large
negative number. Instead:

```
crossDayDelta = (finalPreviousDayTotal − previousDailyValue) + currentDailyValue
```

— the unobserved remainder of yesterday plus today's accumulation so far, guarded
by `finalPreviousDayTotal >= previousDailyValue`.

Worked example: last reading yesterday 8.732, LG's finalized total for yesterday
8.751, first reading today 0.021 → `(8.751 − 8.732) + 0.021 = 0.040`, status
`DAY_ROLLOVER_RESOLVED`, with the finalized total stored on the observation.

When the finalized total is not available yet, the interval is left `null` with
`DAY_ROLLOVER_UNRESOLVED` and the sample is queued in `runtime/reconciliation`.
Later cycles retry — fetching the previous day's total at most once per cycle,
cached in `dailyTotals/{localDate}` so it costs one extra API call per day, not
one per cycle. When it arrives, the observation's **derived** fields are patched
and `RECONCILED` is added; the stored raw values are never touched.

Two ways it ends without a number:

- **Implausible total** — LG reports a "final" total *below* a value already
  observed that day. The reconstruction is refused, `IMPLAUSIBLE_FINAL_TOTAL` is
  flagged, and the value LG returned is recorded for inspection.
- **The 24-hour window closes** — reconciliation stops. The sample stays
  permanently unresolved, which is honest; a fabricated value would not be.

No reconstruction is attempted across more than one day boundary.

---

## The quality model

Three orthogonal things, never one overloaded field.

**`quality.intervalStatus`** — exactly one, explaining why `intervalValue` is
what it is:

| Status | Meaning |
|---|---|
| `NORMAL` | An ordinary same-day difference. |
| `NEW_BASELINE` | Nothing has ever been recorded for this device. |
| `MISSING_PREVIOUS_SAMPLE` | Records exist, but none carries a usable energy value. |
| `COARSE_INTERVAL` | Valid, but spans more than the nominal cadence. |
| `DAY_ROLLOVER_RESOLVED` | Reconstructed across midnight from the finalized total. |
| `DAY_ROLLOVER_UNRESOLVED` | Rollover detected, no trustworthy finalized total. |
| `ANOMALOUS_DECREASE` | The counter went backwards within a day. |
| `MULTI_DAY_GAP` | The baseline predates yesterday. |
| `ENERGY_UNAVAILABLE` | The energy request did not succeed. |

**`quality.flags`** — zero or more independent conditions, as an array so
`array-contains` makes each one queryable on its own:

| Flag | Meaning |
|---|---|
| `RATE_LIMITED` | ThinQ reported the call volume exceeded, or a cooldown suppressed the request. |
| `DEVICE_OFFLINE` | ThinQ reported the device as not connected. |
| `UNCHANGED_COUNTER` | The counter did not move; genuine idleness and provider latency are indistinguishable here. |
| `PARTIAL_OBSERVATION` | Exactly one of the two sources succeeded. |
| `RECONCILED` | A deferred interval was filled in later. |
| `IMPLAUSIBLE_FINAL_TOTAL` | The finalized previous-day total could not be true. |

**`source.energy` and `source.state`** — the outcome of each request,
independently, with the failure class and error code. A failed source is a
property of that source, not of the arithmetic, so it never lands in
`intervalStatus`.

A coarse interval that was also rate limited records **both**: neither displaces
the other, and neither displaces the status. Energy and state are independent —
one failing never discards the other's result, and nothing is ever carried
forward from a previous observation to fill a gap.

### Failure classes

Classification keys on ThinQ's error codes, because the SDK discards the HTTP
response and status is not observable.

| Class | Triggered by | Response |
|---|---|---|
| `RATE_LIMITED` | `1305`, `1306`, `1309` | Backoff with jitter, then a cross-cycle cooldown |
| `AUTH_FATAL` | `1103`, `1218`, `1301`, `1302` | **No retry.** Replace the token |
| `DEVICE_OFFLINE` | `1222` | Not a collector fault; recorded as unavailability |
| `TRANSIENT` | `2000`, `2209`, `2210`, `2212`, `2214` | Bounded retry within the cycle |
| `CONFIG_FATAL` | `1205`, `1212`, `1213`, `1219`–`1221`, `1224`, `1307` | No retry; surfaced loudly |
| `MALFORMED` | Non-JSON body, unexpected structure | Recorded; never crashes the process |
| `TRANSPORT` | Connection errors, timeouts | Bounded retry |
| `UNKNOWN` | Any unmapped code | Recorded, not retried |

---

## Idempotency

Sample identity is the slot, so re-running a slot reconciles rather than
duplicates. Writes are transactional and follow completeness precedence, where
`completeness = 2×(energy ok) + 1×(state ok)`:

| Situation | Result |
|---|---|
| No document for the slot | created |
| The retry is more complete | upgraded |
| The retry is equally complete | no-op — first writer wins, `observedAt` does not churn |
| The retry is less complete | refused, and the refusal is logged |
| Reconciliation | patches derived fields only; never touches raw values |

Deterministic identifiers alone would prevent duplicate *creation* while opening
a stale-overwrite hazard: a delayed retry for slot T could clobber a better
record written since. Precedence closes that, which is what makes replay safe on
a runtime that occasionally double-delivers.

Two cycles that overlap are prevented by a short advisory lease on
`runtime/collector.leaseUntil`. A cycle that cannot take the lease exits without
writing; the gap it leaves is handled as `COARSE_INTERVAL`, which has to work
anyway.

---

## Inspection commands

```
$ airchive latest --limit 3
sampleId           observedAt                   raw   interval     dur  intervalStatus     sources
20260820T091000Z   2026-08-20 09:10:00Z       2.200      0.050    300s  NORMAL             energy=ok state=ok
20260820T090500Z   2026-08-20 09:05:00Z       2.150      0.050    300s  NORMAL             energy=ok state=ok
20260820T090000Z   2026-08-20 09:00:00Z       2.100          —       —  NEW_BASELINE       energy=ok state=ok
```

`health` prints the current health record and any pending reconciliations.
`anomalies --since ... --until ...` returns observations whose status or flags
indicate a problem (defaults to the last 24 hours). `compare` diffs the newest
stored observation against a fresh live reading, marking differing fields with
`*`, and writes nothing.

The Firestore console remains usable for visual confirmation: raw payloads are
stored as readable maps rather than JSON strings specifically so they can be
spot-checked there.

---

## Collector health

`devices/{deviceId}/runtime/collector`, overwritten in place, never appended to
the series and never used as an analytics source:

| Field | Meaning |
|---|---|
| `lastAttemptAt` | Start of the most recent cycle |
| `lastSuccessAt`, `lastSampleId`, `lastSamplePath` | The most recent cycle that stored something |
| `lastErrorAt`, `lastErrorClass`, `lastErrorMessage` | The most recent failure |
| `consecutiveFailures` | Increments per failed cycle, resets to 0 on success |
| `consecutiveRateLimits`, `rateLimitedUntil` | Cross-cycle rate-limit cooldown |
| `leaseUntil`, `leaseHolder` | The overlap-prevention lease |
| `collectorVersion` | Which build wrote it |

Worth alerting on: `consecutiveFailures` climbing, and `lastErrorClass` being
`AUTH_FATAL` or `CONFIG_FATAL` — those never recover on their own.

---

## Rate limiting

ThinQ signals rate limiting in-band via error codes; there is no `Retry-After`
to read, because the SDK discards the HTTP response. So:

1. Within a cycle, a rate-limited request backs off exponentially with ±50%
   jitter, bounded in both attempts and total time.
2. Across cycles, `consecutiveRateLimits` drives a cooldown (10 minutes,
   doubling, capped at an hour) recorded in health. A cycle starting inside the
   cooldown issues **no requests at all** — that is what reduces the effective
   rate rather than merely re-pacing it. It still writes an observation, flagged
   `RATE_LIMITED`, so the outage is visible in the series.
3. The first successful cycle clears the cooldown and normal cadence resumes.

A routine cycle costs exactly two API calls, so one device is ~576 calls a day,
plus one extra on each day boundary for the previous day's finalized total.

---

## Credential hygiene

`ThinQAPIException` is constructed by the SDK with the **outbound request
headers**, which contain `Authorization: Bearer <PAT>`. Any ordinary
`logger.exception(...)`, structured logger walking `__dict__`, or error-reporting
integration would put the token into retained logs. This is the default
behaviour of ordinary code against this specific exception type, not a
hypothetical.

The mitigation is structural. One module touches the SDK. It converts every
exception into a `ThinqFailure` carrying only the failure class, code, error
name, and a safe message, and re-raises it in a way that leaves the original
unreachable through `__cause__` *or* `__context__`. A registered-secret scrubber
backs that up on every log record and printed line. Tests assert a sentinel token
appears in no rendered log record, no formatted traceback, no serialized
failure, and no command's output.

Beyond that: no key file in the image, no key file in the repository, Firestore
client rules denied, `roles/datastore.user` and nothing more, and the PAT from
Secret Manager at runtime.

---

## Known device and API limitations

Each of these is a fact about LG's API or SDK, established by reading
`thinqconnect` 1.0.13 or by discovery against the real device.

- **No instantaneous power property.** The AC state resource has no watts. The
  `DAILY` counter is the only energy signal, which is why the whole design is
  "sample a cumulative counter often".
- **HTTP status is invisible.** `async_request` returns `payload["response"]` and
  discards the `ClientResponse`, so status codes and `Retry-After` never reach
  us. Failure classification keys on ThinQ error codes instead.
- **Errors carry the bearer token.** See [credential hygiene](#credential-hygiene).
- **A non-JSON error body raises a different exception.** `await response.json()`
  is called *before* the `response.ok` check, so a gateway HTML page or an empty
  502 surfaces as `aiohttp.ContentTypeError`, not `ThinQAPIException`. Both are
  handled.
- **The device wrapper is coupled to the process clock.** `ConnectBaseDevice.get_daily_energy_usage()`
  validates dates against `date.today()` — the *system-local* date — and raises
  `ValueError` when `today < end_date`. A UTC-clocked container asking for
  Manila's "today" would fail for the first eight hours of every Manila day,
  precisely when rollover reconciliation runs. This collector uses the low-level
  `ThinQApi`, which performs no such check; a test demonstrates both the coupling
  and its absence from our path.
- **Firestore rejects descending key scans.** "Latest sample is a descending key
  scan" does not work; ordering uses the mirrored `sampleId` field instead.
- **The SDK eagerly imports its MQTT client.** `thinqconnect/__init__.py` imports
  `mqtt_client`, pulling `awsiotsdk` and `pyOpenSSL` — roughly 430 ms and 400
  modules on every cold start for functionality this collector does not use.
  Importing a submodule does not avoid it. Unavoidable while using the official
  SDK; the mitigation is a slim base image.
- **The energy response shape is read defensively.** The extractor locates the
  numeric reading under the names ThinQ plausibly uses and returns *nothing*
  rather than a guess when it cannot. It will never attribute another day's value
  to the requested day. Record the real shape in
  [discovery-findings.md](discovery-findings.md) once observed.
- **Which state properties this model populates is device-specific.** The
  collector stores exactly what the device returns and invents nothing. Anything
  the profile does not expose is simply absent — see the discovery findings.

---

## Storage growth

~288 observations a day, ~105,000 a year, at roughly 2–4 KB each (raw payloads
dominate) → **about 300–450 MB a year**. Firestore's 1 GiB free tier covers
roughly two years. Write volume is negligible; storage is the eventual cost
driver, which is why raw payloads are exempted from indexing.

When it matters, the options are export to GCS or BigQuery, or a cold-storage
tier for older years. Whichever is chosen, **raw observations are never deleted
or downsampled in place.** Any archival mechanism must preserve the raw series
in a durable form; the high-resolution history is the thing that cannot be
recreated.

---

## Future enhancements

- **ThinQ MQTT / event subscription.** The SDK supports push notification and
  event subscription, which would give finer resolution on *state changes* than
  five-minute sampling. It is explicitly **not** used or required here: the
  collector relies on periodic sampling only, and nothing in the implementation
  depends on MQTT. (Its transitive dependencies are dead weight in the image, as
  noted above.)
- **A second device.** The data model is keyed per device, so another device is
  additive, but the runtime targets one. Multi-device orchestration is
  deliberately unbuilt.
- **Aggregation and analytics.** Out of scope by design. The raw series is the
  source of truth and any later rollup derives from it.

---

## Testing

```bash
python -m pytest -q                      # everything, offline, no credentials
python -m ruff check src tests

# The persistence rules again, against a real Firestore:
firebase emulators:exec --only firestore --project demo-airchive \
    ".venv\Scripts\python.exe -m pytest tests/test_store_emulator.py -q"
```

The offline suite substitutes both external systems: a scripted ThinQ API that
records every call, and an in-memory Firestore that models transaction retries
under concurrent writes. Deltas, classification, idempotency, restart recovery,
backoff, and shutdown are all verified without a live service. The emulator
suite re-checks the persistence rules against the real client library — it is
what caught the descending-key-scan assumption.
