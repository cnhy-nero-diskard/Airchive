## 1. Repository and project skeleton

- [x] 1.1 Create the Python project skeleton (Python 3.12+, `pyproject.toml`, `src/` layout, package name) and verify `pip install -e .` succeeds in a clean virtual environment
- [x] 1.2 Add runtime dependencies `thinqconnect` and `google-cloud-firestore`, plus dev dependencies for testing and linting, and verify `python -c "from thinqconnect.thinq_api import ThinQApi"` succeeds
- [x] 1.3 Add `.gitignore` covering credential files, `.env`, and virtual environments, and verify `git status --ignored` shows a placeholder `.env` and a placeholder `*.json` key as ignored
- [x] 1.4 Add `.env.example` enumerating every supported variable (`LG_THINQ_PAT`, `LG_COUNTRY_CODE`, `LG_CLIENT_ID`, `LG_DEVICE_ID`, `LG_ENERGY_PROPERTY`, `FIREBASE_PROJECT_ID`, `POLL_INTERVAL_SECONDS`, `LG_DAY_TIMEZONE`, `LOG_LEVEL`, and local-only `GOOGLE_APPLICATION_CREDENTIALS`) with no real secrets, and verify by grepping the file for any value resembling a token
- [x] 1.5 Add the CLI entry point with subcommand stubs (`discover`, `validate-counter`, `check-firestore`, `poll`, `latest`, `health`, `anomalies`, `compare`) and verify `--help` lists all of them

## 2. Firebase environment — operator-gated

- [x] 2.1 Write the setup guide covering Firebase project creation (suggested `lg-ac-telemetry`), enabling Cloud Firestore Standard, and region selection, stating explicitly that the region is permanent once created; verify the guide is committed and a reader can follow it without prior context
- [ ] 2.2 Walk the operator through creating the project and database per the guide, and verify the database is visible in the Firebase Console
- [ ] 2.3 Set Firestore client security rules to deny all client access and verify a client-side read attempt is rejected while server-side Admin access still succeeds
- [ ] 2.4 Document and perform local Application Default Credentials setup (`gcloud auth application-default login`), with a documented escape hatch and revocation steps for `GOOGLE_APPLICATION_CREDENTIALS`; verify no key file exists inside the repository
- [ ] 2.5 Implement `check-firestore` performing a write/read/delete round trip against a scratch document, and verify it exits zero locally and the document was observable in the Console before deletion
- [ ] 2.6 **Gate:** confirm `check-firestore` passes before any collector logic is implemented, and record the confirmation in the setup guide

## 3. ThinQ access and discovery — findings-gated

- [x] 3.1 Document obtaining the ThinQ Personal Access Token, determining the country code, and generating the client ID exactly once; verify the guide states the client ID is persisted and reused, never regenerated per run
- [x] 3.2 Implement configuration loading and startup validation that fails with a message naming every missing or malformed value and emits no secrets; verify unit tests cover missing PAT, missing client ID, and malformed interval
- [x] 3.3 Implement the sanitizing SDK boundary converting `ThinQAPIException`, `aiohttp` errors, and timeouts into an internal failure type carrying only failure class, code, error name, and safe message; verify a test using a sentinel PAT asserts the token appears in no rendered log record or serialized failure
- [x] 3.4 Implement the failure classification table from design D3 over ThinQ error codes; verify unit tests map `1306`, `1103`, `1222`, `2210`, `1219`, a non-JSON body, and a timeout to their expected classes
- [ ] 3.5 Implement `discover` listing devices once, identifying air-conditioner candidates with identifier, alias, model name, and type, and reporting device profile, energy profile, current state, and today's daily usage; verify it runs read-only against the real account and issues no control command
- [x] 3.6 Add startup validation of the configured energy property against `energy_profile["result"]["property"]`, and verify an unsupported property fails startup with a message naming the supported ones
- [ ] 3.7 Implement `validate-counter` sampling the current-day value repeatedly and reporting the observed sequence, whether it advances intraday, apparent update latency, and numeric precision; verify it issues no control command
- [ ] 3.8 Run `validate-counter` across several hours of normal air-conditioner use and record the findings
- [ ] 3.9 Determine the effective LG day boundary from observed counter resets plus any timezone the API exposes, and verify the finding is recorded and any mismatch with `LG_DAY_TIMEZONE` is reported to the operator
- [ ] 3.10 **Gate:** confirm the current-day counter advances intraday. If it does not, stop and revisit the proposal before implementing energy-delta behavior
- [ ] 3.11 Record all discovery findings in the documentation — confirmed energy property, unit, precision, update latency, day boundary, and the readable properties this device actually exposes — and verify the provisional schema is reconciled against them

## 4. Observation model

- [x] 4.1 Implement the observation data model with the three-part quality representation from design D5 (`quality.intervalStatus`, `quality.flags`, per-source `source.energy`/`source.state`), and verify a test asserts two independent conditions can coexist without displacing each other
- [x] 4.2 Implement `Decimal`-based energy parsing and quantization to the validated precision, and verify a test asserts `8.751 − 8.732` yields exactly `0.019` with no float artifact
- [x] 4.3 Implement same-day delta calculation using actual `observedAt` timestamps for duration, and verify tests cover the normal increment (2.100 → 2.150 = 0.050) and the unchanged counter (2.100 → 2.100 = 0, flagged `UNCHANGED_COUNTER`)
- [x] 4.4 Implement baseline handling, and verify tests distinguish `NEW_BASELINE` (no prior observation ever) from `MISSING_PREVIOUS_SAMPLE` (prior observation without usable energy), both yielding a null interval with the raw value retained
- [x] 4.5 Implement coarse-interval handling for missed cycles, and verify a test asserts 12:00 → 12:15 (2.100 → 2.190) yields 0.090 over ~900 seconds marked `COARSE_INTERVAL`, never the nominal 300 seconds
- [x] 4.6 Implement same-day decrease handling, and verify a test asserts 3.500 → 3.200 yields a null interval with `ANOMALOUS_DECREASE` and both raw values retained
- [x] 4.7 Implement day-rollover detection against the validated day boundary, and verify a test asserts 8.732 → 0.021 across midnight never produces a negative interval
- [x] 4.8 Implement cross-midnight reconstruction `(finalPrevious − previousDaily) + currentDaily` guarded by `finalPrevious >= previousDaily`, and verify a test asserts 8.732 / 8.751 / 0.021 yields exactly 0.040 marked `DAY_ROLLOVER_RESOLVED`
- [x] 4.9 Implement unresolved-rollover handling when the finalized total is unavailable or implausible, and verify tests assert a null interval with `DAY_ROLLOVER_UNRESOLVED`, and `IMPLAUSIBLE_FINAL_TOTAL` when the guard fails
- [x] 4.10 Implement multi-day-gap handling, and verify a test asserts no reconstruction is attempted across more than one day boundary and the status is `MULTI_DAY_GAP` with the raw value retained
- [x] 4.11 Implement partial-success handling for independent energy and state outcomes, and verify tests cover energy-ok/state-failed, state-ok/energy-failed, and both-failed with no fabricated values and no carrying forward of prior state
- [x] 4.12 Implement state normalization preserving every readable property this device exposes, including the temperature unit, without inventing absent properties; verify a test asserts absent profile properties are omitted rather than defaulted

## 5. Persistence

- [x] 5.1 Implement the Firestore layout from design D8 (`telemetry`, `dailyTotals`, `metadata`, `runtime`) and verify documents land at the expected paths against the Firestore emulator or a scratch collection
- [x] 5.2 Implement UTC-floored slot sample IDs, and verify a test asserts the 17:15 Asia/Manila slot yields `20260820T091500Z` and that IDs sort lexicographically in chronological order
- [x] 5.3 Implement transactional idempotent writes with completeness precedence per design D7, and verify tests assert: repeated execution of one slot creates exactly one document; a more complete retry upgrades it; an equal retry is a no-op; a less complete late write is refused
- [x] 5.4 Store `scheduledAt`, `observedAt`, and `persistedAt` as native Firestore timestamps plus `localDate` and the timezone used, and verify a range query over `observedAt` and a grouping by `localDate` both work
- [x] 5.5 Implement raw payload retention (`raw.energy`, `raw.state`) excluding all credential material, and verify a test asserts no authorization header or token is present in a persisted document
- [ ] 5.6 Configure the single-field index exemption on `raw` with array and map descent disabled, commit the index configuration, and verify the exemption is active in the Firestore console
- [x] 5.7 Implement the bounded descending previous-reading lookup per design D12, and verify tests cover finding a usable baseline, skipping an unusable most-recent observation, and exhausting the window
- [x] 5.8 Implement versioned profile metadata caching separate from the observation series, and verify an observation embeds no full profile while referencing the active metadata version
- [x] 5.9 Implement the `dailyTotals` cache for finalized per-day totals, and verify a test asserts the finalized total is fetched once per day and reused rather than re-requested per cycle
- [x] 5.10 Implement the mutable collector health record (last attempt, last success, last sample, last error and class, consecutive failures, collector version), and verify it is overwritten in place, never appended to the series, and that the failure counter resets on success
- [x] 5.11 Implement bounded deferred reconciliation via `runtime/reconciliation`, and verify tests assert an unresolved rollover is filled in when the finalized total arrives, is marked `RECONCILED` without altering stored raw values, and is abandoned after the 24-hour window

## 6. Runtime

- [ ] 6.1 Implement the polling cycle orchestration issuing exactly one energy request and one state request per cycle, and verify a test asserts no device-list, device-profile, or energy-profile call occurs on a routine cycle with warm metadata
- [ ] 6.2 Implement bounded retries with exponential backoff and jitter, explicit per-request timeouts, and a total budget that keeps a cycle inside its interval; verify tests assert bounded attempts and that no tight retry loop occurs
- [ ] 6.3 Implement rate-limit backoff on the `RATE_LIMITED` class, and verify a test asserts bounded backoff with jitter, the `RATE_LIMITED` flag on the sample, and reduced effective request rate under sustained limiting
- [ ] 6.4 Implement fatal-condition handling so `AUTH_FATAL` and `CONFIG_FATAL` consume no retries and surface distinctly in health, and verify a test asserts a revoked token is not retried with backoff
- [ ] 6.5 Implement cycle-level error containment so a malformed response, unexpected exception, or Firestore failure ends only that cycle; verify a test asserts the process survives and the next cycle proceeds
- [ ] 6.6 Implement restart and stateless-invocation recovery reconstructing the previous reading from Firestore, and verify tests assert 4.210 → 4.370 across a gap yields 0.160 over the actual elapsed duration marked `COARSE_INTERVAL`, and that a prior-day baseline routes to rollover handling
- [ ] 6.7 Implement the advisory lease preventing overlapping cycles per design D14, and verify a test asserts a second concurrent cycle exits without writing
- [ ] 6.8 Implement graceful shutdown leaving no partial observation, and verify a test asserts a termination signal mid-cycle produces either a complete record or none
- [ ] 6.9 Implement structured logging with the sample ID as correlation identifier carrying all fields required by the runtime spec, and verify a test asserts every required field is present and no secret is
- [ ] 6.10 Add a test asserting the energy request path succeeds with the process clock in UTC while requesting a Manila-local date, proving the low-level `ThinQApi` choice avoids the SDK's `date.today()` coupling
- [ ] 6.11 Implement test doubles for the ThinQ API and Firestore so all behavior above is verifiable without live credentials, and verify the full suite passes offline

## 7. Operator tooling

- [ ] 7.1 Implement `latest` listing recent observations in reverse time order with sample ID, observation time, raw value, interval and duration, interval status, and source outcomes; verify against seeded data
- [ ] 7.2 Implement `health` reporting the current collector health record, and verify it reflects a seeded failure state including consecutive failures and last error class
- [ ] 7.3 Implement `anomalies` returning observations whose interval status or flags indicate a problem over a requested range, and verify it surfaces seeded `ANOMALOUS_DECREASE` and `DAY_ROLLOVER_UNRESOLVED` records
- [ ] 7.4 Implement `compare` diffing the latest stored observation against a fresh live reading, and verify it performs no write to the telemetry series
- [ ] 7.5 Verify all operator command output is free of tokens and credentials by running each subcommand with a sentinel PAT and grepping the output

## 8. Deployment

- [ ] 8.1 Add a container image with a slim base, and verify it builds and runs `poll --once` locally against ADC
- [ ] 8.2 Create the deployment service account limited to `roles/datastore.user`, and verify the collector can write telemetry and cannot perform unrelated project operations
- [ ] 8.3 Store the ThinQ PAT in Secret Manager and inject it at runtime, and verify no credential material is present in the built image
- [ ] 8.4 Deploy the Cloud Run Job using the attached service account with no key file, and verify a manual execution writes one observation
- [ ] 8.5 Create the Cloud Scheduler trigger at the five-minute cadence, and verify three consecutive scheduled executions produce three distinct sequential sample documents
- [ ] 8.6 Verify runtime logs appear in Cloud Logging and that a stored observation can be traced to its originating log records by sample ID
- [ ] 8.7 Record the deployment topology evaluation against cost, reliability, cadence, credential handling, restart behavior, observability, and rate limits, and verify the rationale is captured in the documentation

## 9. Documentation and closeout

- [ ] 9.1 Write the operations documentation covering project and database creation, local credentials, PAT and client ID setup, discovery, every environment variable, local and deployed execution, the stored data model, inspection commands, collector health, and rate-limit behavior; verify a reader can operate the system from it alone
- [ ] 9.2 Document interval and delta semantics, day-rollover reconciliation, the quality model, and idempotency behavior, and verify each documented status and flag matches the implemented closed sets
- [ ] 9.3 Document known device and API limitations — no instantaneous power property, the SDK's eager MQTT imports, the `date.today()` coupling avoided, invisible HTTP status, and any properties this model does not expose — and verify each is traceable to a discovery finding or design decision
- [ ] 9.4 Document the projected storage growth and the deferred archival options, stating that raw observations are never deleted in place
- [ ] 9.5 Record MQTT/event subscription as a future enhancement explicitly not required by this change, and verify it is not referenced as a dependency anywhere in the implementation
- [ ] 9.6 Run `openspec validate add-lg-aircon-telemetry-poller --strict` and verify it passes
- [ ] 9.7 Confirm the collector has run unattended for at least 24 hours across a midnight boundary, and verify the series contains a resolved or explicitly unresolved rollover sample and no negative interval
