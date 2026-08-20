## Purpose

Defines how observations are stored so the historical series remains trustworthy for years: an append-only observation series keyed by deterministic identifiers, idempotent writes that reconcile rather than duplicate, separately versioned static metadata, a mutable health record kept apart from history, and an indexing policy that keeps rich raw payloads affordable.

## ADDED Requirements

### Requirement: Server-side persistence with locked-down client access

Telemetry SHALL be persisted to a dedicated document database project used exclusively by this collector. Access SHALL be server-side only.

#### Scenario: Client access rules are configured

- **WHEN** the database security rules are established
- **THEN** they SHALL deny all direct client access
- **AND** they SHALL NOT be opened to accommodate a frontend, because no frontend exists in this change

#### Scenario: The collector authenticates

- **WHEN** the collector reads or writes telemetry
- **THEN** it SHALL do so with server-side administrative credentials obtained from the ambient environment
- **AND** a long-lived credential file MUST NOT be bundled with the deployed application

### Requirement: Observation series is the source of truth

The historical observation series SHALL be an append-only collection of per-device records, each representing one sampling occasion. It SHALL be the authoritative record from which all later analysis derives.

#### Scenario: Records are organized per device

- **WHEN** an observation is persisted
- **THEN** it SHALL be stored under a path scoped to its device identifier
- **AND** the series SHALL remain queryable in time order

#### Scenario: Raw observations are retained indefinitely

- **WHEN** aggregated or derived views are introduced at any later time
- **THEN** the original high-resolution observations MUST NOT be deleted or downsampled in place
- **AND** any retention or archival mechanism SHALL preserve the raw series in some durable form

### Requirement: Deterministic sample identifiers

Each observation SHALL be addressed by an identifier derived deterministically from the sampling slot it represents. Auto-generated identifiers MUST NOT be used for scheduled observations.

#### Scenario: An identifier is derived

- **WHEN** a polling cycle is scheduled for a given instant
- **THEN** the observation's identifier SHALL be derived from that slot in a fixed, timezone-unambiguous, lexicographically sortable form
- **AND** the same slot SHALL always produce the same identifier

#### Scenario: A cycle runs late

- **WHEN** a cycle intended for a slot actually executes some minutes after that slot
- **THEN** the identifier SHALL still correspond to the intended slot
- **AND** both the scheduled instant and the actual observation instant SHALL be recorded on the observation

### Requirement: Idempotent writes with defined precedence

Repeated execution for the same sampling slot MUST NOT create duplicate observations. Writes SHALL follow defined precedence rules so that a retry cannot degrade an already-recorded observation.

#### Scenario: The same slot is processed twice

- **WHEN** a cycle for a slot is retried after a partial or uncertain outcome
- **THEN** exactly one observation document SHALL exist for that slot
- **AND** the retry SHALL reconcile the existing document rather than create another

#### Scenario: A retry carries a more complete result

- **WHEN** an existing observation for a slot recorded a failed data source and the retry successfully retrieves that source
- **THEN** the observation SHALL be updated to the more complete result

#### Scenario: A late write carries a less complete result

- **WHEN** a delayed or duplicated execution would overwrite an existing observation with a result that is less complete or less trustworthy than what is already stored
- **THEN** the existing observation SHALL be preserved
- **AND** the less complete result MUST NOT replace it

#### Scenario: Concurrent executions target the same slot

- **WHEN** two executions attempt to write the same slot at overlapping times
- **THEN** the outcome SHALL be a single document consistent with the precedence rules
- **AND** the series MUST NOT be left with a torn or partially applied record

### Requirement: Queryable native timestamps

Timestamps used for ordering or filtering SHALL be stored as native database timestamp values, not solely as formatted strings.

#### Scenario: An observation records its times

- **WHEN** an observation is persisted
- **THEN** the scheduled instant, the observation instant, and the persistence instant SHALL each be stored as native timestamp values
- **AND** the local calendar day SHALL additionally be stored in a form suitable for grouping by day
- **AND** the timezone used to determine that local day SHALL be recorded

### Requirement: Static metadata is stored separately from observations

Device metadata and capability profiles SHALL be cached separately from the observation series. They MUST NOT be duplicated into every observation.

#### Scenario: Profiles are cached

- **WHEN** discovery or a metadata refresh retrieves the device profile and energy profile
- **THEN** they SHALL be persisted in a metadata location distinct from the observation series
- **AND** the time of retrieval SHALL be recorded

#### Scenario: An observation is written

- **WHEN** an observation is persisted
- **THEN** it MUST NOT embed the full static device or energy profile
- **AND** it MAY reference the metadata version in effect at that time

#### Scenario: A profile changes

- **WHEN** a refreshed profile differs from the cached profile
- **THEN** the change SHALL be retained in a way that allows a historical observation to be interpreted against the profile that was in effect when it was recorded

### Requirement: Raw payload retention without secret leakage

Each observation SHALL retain a compact representation of the useful raw source payloads alongside its normalized fields, so the dataset can be reinterpreted later. Retained payloads MUST exclude all credential material.

#### Scenario: Raw payloads are retained

- **WHEN** an observation is persisted following a successful retrieval
- **THEN** it SHALL retain the raw energy payload and the raw state payload as returned by the API
- **AND** those payloads SHALL be stored in a form that permits later reinterpretation

#### Scenario: Credentials are excluded

- **WHEN** raw payloads are retained
- **THEN** authorization headers, tokens, and any other secrets MUST NOT be stored
- **AND** only response content SHALL be retained, never request credentials

### Requirement: Intentional indexing policy

Indexing SHALL be configured deliberately. Fields intended for later analytical querying SHALL be indexed; bulky raw payloads that will never be queried directly SHALL be excluded from indexing.

#### Scenario: Analytical fields are queryable

- **WHEN** the data model is established
- **THEN** the observation instant, the local day, the interval status, the independent condition markers, and the principal state fields such as operating mode, target temperature, and power-saving setting SHALL be efficiently queryable

#### Scenario: Raw payloads are excluded from indexing

- **WHEN** raw payloads are retained on each observation
- **THEN** they SHALL be excluded from automatic indexing, including any nested subfields
- **AND** the growth of index storage MUST NOT scale with the internal structure of retained raw payloads

#### Scenario: Storage growth is understood

- **WHEN** the data model is established
- **THEN** the expected per-observation size and annual storage growth SHALL be documented
- **AND** long-term archival options SHALL be recorded as a future concern without deleting raw history

### Requirement: Previous-reading recovery from storage

The previous comparable reading SHALL be recoverable from persisted data rather than depending on in-memory state.

#### Scenario: A stateless invocation begins

- **WHEN** a cycle starts with no in-process memory of prior cycles
- **THEN** it SHALL determine the most recent usable prior observation by querying the stored series
- **AND** it SHALL establish that observation's raw cumulative value and local day for interval calculation

#### Scenario: The most recent stored observation is unusable as a baseline

- **WHEN** the most recent stored observation carries no usable raw energy value
- **THEN** the lookup SHALL continue to earlier observations within a bounded window to find a usable baseline
- **AND** if none is found, the cycle SHALL proceed with no previous reading rather than using an unusable one

### Requirement: Collector health is separate from history

A small mutable record SHALL express current collector health for quick operational inspection. It MUST NOT be treated as, or mixed into, the historical observation series.

#### Scenario: Health is updated

- **WHEN** a cycle completes, successfully or otherwise
- **THEN** the health record SHALL be updated to reflect at least the last attempt time, the last success time, the last written sample identifier, the last error time, the last error class, the count of consecutive failures, and the collector version

#### Scenario: Health is not history

- **WHEN** the health record is updated
- **THEN** it SHALL be overwritten in place
- **AND** it MUST NOT be appended into the observation series or used as a source for analytics

#### Scenario: Consecutive failures accumulate and reset

- **WHEN** successive cycles fail
- **THEN** the consecutive failure count SHALL increase
- **AND** it SHALL reset upon the next successful cycle
