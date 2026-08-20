## Purpose

Provides authenticated, rate-limit-aware, read-only access to the official LG ThinQ Connect API for a single air-conditioner, including one-time device discovery, cached static profiles, a failure taxonomy that distinguishes retryable from fatal conditions, and credential hygiene at the API boundary.

## ADDED Requirements

### Requirement: Official API only

The collector SHALL obtain all device data through the official LG ThinQ Connect API using LG's officially published SDK. The collector MUST NOT use reverse-engineered ThinQ clients, legacy unofficial ThinQ endpoints, unofficial authentication flows, or any form of traffic interception or proxying of the ThinQ mobile application.

#### Scenario: An unofficial source would expose more properties

- **WHEN** an unofficial client or intercepted mobile-app traffic would expose device properties not available through the official API
- **THEN** the collector SHALL NOT use that source
- **AND** the additional properties SHALL be recorded as a known API limitation rather than obtained by unsupported means

#### Scenario: Read-only operation

- **WHEN** the collector interacts with the device for any purpose, including discovery, validation, and routine polling
- **THEN** it SHALL issue only read operations
- **AND** it SHALL NOT issue device control commands unless a human operator explicitly requests a control action

### Requirement: Configuration inputs and startup validation

The collector SHALL accept its ThinQ credentials and target device as external configuration: a Personal Access Token, a country code, a client ID, and a device identifier. It SHALL validate all required configuration at startup, before any network call or write.

#### Scenario: Required configuration is missing or malformed

- **WHEN** the collector starts and a required configuration value is absent, empty, or structurally invalid
- **THEN** it SHALL fail immediately with a message naming each offending value
- **AND** it SHALL NOT attempt any ThinQ or Firestore operation
- **AND** the message SHALL NOT contain any secret value

#### Scenario: A configuration template is provided

- **WHEN** an operator prepares a new environment
- **THEN** a configuration template enumerating every supported variable SHALL be available in the repository
- **AND** that template SHALL contain no real secret values

### Requirement: Stable client identity

The ThinQ client ID SHALL be generated once and persisted as configuration. The collector MUST reuse the same client ID across every invocation and restart.

#### Scenario: A scheduled invocation starts

- **WHEN** the collector begins any polling cycle, including the first cycle after a restart or a fresh stateless invocation
- **THEN** it SHALL use the client ID supplied by configuration
- **AND** it SHALL NOT generate a new client ID

#### Scenario: Client ID is absent from configuration

- **WHEN** no client ID is configured
- **THEN** startup validation SHALL fail and instruct the operator to generate one and persist it
- **AND** the collector SHALL NOT silently generate an ephemeral identity to proceed

### Requirement: Credentials are never exposed in output

No log record, persisted document, error message, health record, or diagnostic output SHALL contain the ThinQ Personal Access Token, Google credentials, an authorization header, or any other secret.

#### Scenario: An API error carries request headers

- **WHEN** the underlying SDK raises an API error whose attached data includes the outbound request headers, which carry the bearer token
- **THEN** the collector SHALL convert that error into a sanitized internal representation retaining only the error code, the error name, and a safe message
- **AND** the original error object and its attributes MUST NOT be passed to any logging, serialization, or persistence path

#### Scenario: An unexpected exception propagates

- **WHEN** an unexpected exception occurs anywhere in a polling cycle
- **THEN** the emitted diagnostic output SHALL be free of secret material
- **AND** a secret appearing in output SHALL be treated as a defect

### Requirement: Failure taxonomy

The collector SHALL classify every ThinQ failure into a stable class that determines its response. The classes SHALL distinguish, at minimum: rate limiting, authentication failure, device unavailability, transient server-side conditions, malformed or non-conforming responses, and transport failures.

#### Scenario: Rate limiting is reported

- **WHEN** the API reports that the permitted call volume has been exceeded, or that the API call is not currently allowed
- **THEN** the failure SHALL be classified as rate limiting
- **AND** the collector SHALL back off as specified by the runtime capability rather than retrying immediately

#### Scenario: The token is invalid or absent

- **WHEN** the API reports an invalid, expired, or unknown token
- **THEN** the failure SHALL be classified as fatal authentication failure
- **AND** the collector SHALL NOT treat it as a transient condition to be retried with backoff
- **AND** the condition SHALL be recorded distinctly in collector health so an operator can see that credentials need replacement

#### Scenario: The device is not connected

- **WHEN** the API reports that the device is not connected
- **THEN** the failure SHALL be classified as device unavailability, distinct from an API or transport failure
- **AND** the observation SHALL record that the device was unreachable rather than that the collector malfunctioned

#### Scenario: A transient server-side condition is reported

- **WHEN** the API reports an internal error, a device response delay, a synchronizing state, or an explicit retry instruction
- **THEN** the failure SHALL be classified as transient
- **AND** it SHALL be eligible for bounded retry within the cycle

#### Scenario: The response body is not valid structured data

- **WHEN** the API returns a response whose body cannot be parsed as the expected structured format, such as a gateway error page
- **THEN** the collector SHALL classify it as a malformed response rather than allowing the parse failure to escape as an unhandled error
- **AND** the polling cycle SHALL terminate with a recorded failure instead of crashing the process

#### Scenario: Rate-limit metadata is unavailable

- **WHEN** the collector must decide how long to wait after a rate-limit classification and the API surface exposes no retry-delay hint
- **THEN** the collector SHALL apply its own bounded backoff schedule
- **AND** it SHALL NOT block indefinitely waiting for metadata that will not arrive

### Requirement: Per-cycle call budget

A routine polling cycle SHALL issue only the API calls required to produce that cycle's observation: the current-day energy usage request and the device state request. The device list, the device profile, and the energy profile MUST NOT be requested on every cycle.

#### Scenario: A routine cycle runs with valid cached metadata

- **WHEN** a polling cycle runs and cached device metadata is present and current
- **THEN** the collector SHALL issue exactly one energy-usage request and one device-state request for the target device
- **AND** it SHALL issue no device-list, device-profile, or energy-profile request

#### Scenario: A day boundary has been crossed

- **WHEN** the first cycle of a new local day requires the previous day's finalized total
- **THEN** at most one additional historical energy-usage request SHALL be issued for that purpose
- **AND** that additional request SHALL NOT recur on subsequent cycles of the same day once the value is resolved

### Requirement: Device discovery and cached static metadata

The collector SHALL support an explicit discovery operation that identifies the target air conditioner and captures its static metadata. Static metadata SHALL be cached and refreshed only on demand or on a slow schedule, never on each polling cycle.

#### Scenario: Discovery is run

- **WHEN** an operator runs discovery
- **THEN** the collector SHALL list registered devices once, identify devices of the air-conditioner type, and report each candidate's device identifier, alias, model name, and device type
- **AND** it SHALL retrieve and report the device profile, the energy profile, and the current state

#### Scenario: The energy property is determined from the profile

- **WHEN** discovery inspects the energy profile
- **THEN** it SHALL report the energy properties the device actually supports and the units and precision observed in an actual usage response
- **AND** the energy property name used for polling SHALL be taken from configuration derived from that profile
- **AND** the collector MUST NOT fall back to a hard-coded property name that was never observed on this device

#### Scenario: The configured energy property is not supported

- **WHEN** the configured energy property does not appear in the device's energy profile
- **THEN** startup validation SHALL fail with a message naming the supported properties
- **AND** the collector SHALL NOT poll with an unsupported property

#### Scenario: Readable state properties are enumerated

- **WHEN** discovery inspects the device profile and current state
- **THEN** it SHALL report every readable property the device actually exposes
- **AND** properties absent from this device's profile MUST NOT be invented or assumed present

### Requirement: LG day boundary is determined empirically

The local day used to bucket daily energy totals is defined by LG's own accounting for the device, which may differ from the collector's configured timezone. The collector SHALL determine and record the effective day boundary rather than assuming its configured timezone governs it.

#### Scenario: Discovery validates the day boundary

- **WHEN** the discovery and validation phase runs
- **THEN** it SHALL record observations of when the current-day counter resets, and any timezone information the API exposes for the device or account
- **AND** the resulting day boundary SHALL be recorded as configuration used by day-rollover handling

#### Scenario: Observed reset disagrees with configured timezone

- **WHEN** the observed counter reset does not align with midnight in the configured timezone
- **THEN** the discrepancy SHALL be reported to the operator as a finding that affects day-rollover correctness
- **AND** the collector SHALL NOT silently continue with an unverified boundary assumption

### Requirement: Pseudo-live counter behavior is validated before it is relied upon

Because the official API exposes no instantaneous power reading, the collector depends on the current-day cumulative energy counter advancing within the day. This behavior SHALL be validated against the actual target device before the polling design is treated as viable.

#### Scenario: Validation observes the counter over time

- **WHEN** the validation procedure samples the current-day energy value repeatedly while the air conditioner is consuming power under normal use
- **THEN** it SHALL record whether the value changes intraday, the approximate update latency, the numeric precision, the units, evidence of cached or repeated values, and any retroactive change to an already-observed current-day value
- **AND** it SHALL NOT issue control commands to induce consumption

#### Scenario: The counter does not advance intraday

- **WHEN** validation shows the current-day value does not change within the day
- **THEN** the finding SHALL be reported as invalidating sub-daily energy resolution through this API
- **AND** the change SHALL be revisited before the energy-delta behavior is implemented
- **AND** state-only telemetry collection MAY still proceed
