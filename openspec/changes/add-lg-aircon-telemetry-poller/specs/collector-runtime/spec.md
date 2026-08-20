## Purpose

Defines how the collector executes: its sampling cadence, how a cycle degrades rather than crashes, how retries and backoff are bounded, how it recovers after restarts, and how each cycle is traceable through structured logs correlated to the record it produced.

## ADDED Requirements

### Requirement: Headless continuous operation

The collector SHALL run headlessly and continuously with no user-facing interface, and SHALL operate independently of any other application.

#### Scenario: The collector runs

- **WHEN** the collector is deployed and operating
- **THEN** it SHALL require no interactive input and present no user interface
- **AND** it SHALL depend on no other application's runtime, database, or deployment

### Requirement: Sampling cadence

Observations SHALL be attempted on a configurable interval, defaulting to five minutes.

#### Scenario: The default cadence is used

- **WHEN** no interval is configured
- **THEN** the collector SHALL attempt an observation approximately every five minutes

#### Scenario: The cadence is changed

- **WHEN** an operator configures a different interval
- **THEN** the collector SHALL honor it
- **AND** the interval status classification SHALL continue to rely on actual elapsed observation times rather than the configured interval

### Requirement: A single failing cycle never stops collection

A malformed response, an API failure, or a storage failure in one cycle MUST NOT terminate ongoing collection.

#### Scenario: A response cannot be parsed

- **WHEN** the API returns a response the collector cannot interpret
- **THEN** the cycle SHALL end with a recorded failure
- **AND** subsequent cycles SHALL continue on schedule

#### Scenario: An unexpected error occurs

- **WHEN** an unanticipated error arises during a cycle
- **THEN** it SHALL be contained within that cycle, recorded, and reflected in collector health
- **AND** the collector SHALL remain available for the next cycle

#### Scenario: Persistence fails

- **WHEN** the observation cannot be written to storage
- **THEN** the failure SHALL be recorded in the logs for that cycle
- **AND** the collector SHALL continue with subsequent cycles rather than exiting

### Requirement: Bounded retries and backoff

Retries SHALL be bounded in count and duration, SHALL use exponential backoff, and SHALL apply randomized jitter where concurrent or repeated scheduling could otherwise synchronize. A tight retry loop is prohibited.

#### Scenario: A transient failure is retried

- **WHEN** a request fails with a transient classification
- **THEN** it SHALL be retried a bounded number of times with exponentially increasing delay
- **AND** the total time spent SHALL not exceed a bound that keeps the cycle from overrunning its interval

#### Scenario: Rate limiting is encountered

- **WHEN** a request is classified as rate limited
- **THEN** the collector SHALL back off with exponentially increasing delay and jitter
- **AND** it SHALL NOT retry immediately or repeatedly in quick succession
- **AND** the condition SHALL be recorded on the observation and in collector health

#### Scenario: Sustained rate limiting

- **WHEN** rate limiting persists across cycles
- **THEN** the effective request rate SHALL be reduced rather than sustained at the limit
- **AND** the collector SHALL resume normal cadence once requests succeed again

#### Scenario: A fatal condition is not retried

- **WHEN** a failure is classified as fatal, such as invalid credentials or an unsupported configuration
- **THEN** the collector SHALL NOT consume retry attempts on it
- **AND** the condition SHALL be surfaced distinctly so an operator can act

#### Scenario: Requests are time-bounded

- **WHEN** any external request is issued
- **THEN** it SHALL carry an explicit timeout
- **AND** a hung request MUST NOT stall the cycle indefinitely

### Requirement: A cycle overrunning its interval does not pile up

The collector SHALL prevent overlapping work for the same device from corrupting the series or amplifying request volume.

#### Scenario: A cycle is still running when the next is due

- **WHEN** a cycle has not completed by the time the next is scheduled
- **THEN** the collector SHALL either skip or defer the new cycle rather than running unbounded concurrent cycles against the same device
- **AND** any resulting gap SHALL be handled by the coarse-interval behavior rather than by fabricating a value

### Requirement: Restart and invocation recovery

The collector SHALL NOT depend on in-memory continuity. On every startup or invocation it SHALL reconstruct the state needed to continue the series correctly.

#### Scenario: The collector restarts after downtime

- **WHEN** the collector resumes after a period of not running, with a stored reading of 4.210 observed earlier the same local day and a current reading of 4.370
- **THEN** it SHALL derive interval consumption of 0.160 over the actual elapsed duration
- **AND** the interval SHALL be marked as coarser than the nominal cadence

#### Scenario: The collector restarts across a day boundary

- **WHEN** the collector resumes and the most recent stored reading belongs to an earlier local day
- **THEN** day-rollover handling SHALL apply rather than same-day subtraction

#### Scenario: A fresh invocation has no prior process state

- **WHEN** the runtime provides no continuity between invocations
- **THEN** the cycle SHALL still produce a correctly classified observation using only persisted state and the current API responses

### Requirement: Graceful shutdown

Where the runtime signals termination, the collector SHALL shut down without leaving a partially written observation.

#### Scenario: A termination signal arrives mid-cycle

- **WHEN** the runtime signals shutdown while a cycle is in progress
- **THEN** the collector SHALL either complete or abandon the in-flight write such that no partial or inconsistent observation is left in the series
- **AND** it SHALL exit without requiring forced termination

### Requirement: Structured logs correlated to stored records

Every cycle SHALL emit structured logs carrying a stable correlation identifier that ties the logs to the persisted observation.

#### Scenario: A cycle emits logs

- **WHEN** a cycle runs
- **THEN** its structured log records SHALL include at least the sample identifier, the device identifier, the scheduled and observed instants, the outcome of the energy and state requests, the previous and current raw cumulative values, the derived interval consumption and duration, the interval status, the storage path written, and the cycle duration

#### Scenario: A stored record is traced back to its logs

- **WHEN** an operator holds a stored observation and wants the logs that produced it
- **THEN** the correlation identifier on the observation SHALL be sufficient to locate those log records

#### Scenario: Logs and stored telemetry are not conflated

- **WHEN** the collector records information
- **THEN** persisted telemetry and collector state SHALL go to the telemetry database
- **AND** operational and diagnostic logging SHALL go to the runtime's logging system
- **AND** neither SHALL be used as a substitute for the other

#### Scenario: Logs contain no secrets

- **WHEN** any log record is emitted at any level
- **THEN** it SHALL contain no token, credential, or authorization header

### Requirement: Deployment topology is chosen against stated criteria

The execution environment SHALL be selected by evaluating the cadence against cost, reliability, credential handling, restart behavior, observability, and API rate limits. An always-running process MUST NOT be adopted merely because the workload involves polling.

#### Scenario: The topology is decided

- **WHEN** the deployment approach is chosen
- **THEN** the evaluation of the considered options against each stated criterion SHALL be recorded
- **AND** the chosen option SHALL be the simplest one that satisfies them

#### Scenario: Deployed credentials are ambient

- **WHEN** the collector runs in the deployed environment
- **THEN** it SHALL obtain database credentials from the runtime's attached identity
- **AND** a credential file MUST NOT be shipped in the deployment artifact
- **AND** the API token SHALL be supplied from a managed secret store rather than embedded in the artifact or in plain configuration

#### Scenario: Event-driven subscription is not required

- **WHEN** the initial collector is delivered
- **THEN** it SHALL rely on periodic sampling only
- **AND** event or message-based subscription SHALL NOT be a prerequisite for delivery
- **AND** any benefit it might offer for finer state-change resolution SHALL be recorded as a future enhancement

### Requirement: External dependencies are mockable

External API and storage interactions SHALL be replaceable in tests so that behavior can be verified without live services.

#### Scenario: Behavior is tested without live services

- **WHEN** the delta, classification, idempotency, recovery, and backoff behaviors are tested
- **THEN** the external API and the storage layer SHALL be substitutable with controlled test doubles
- **AND** tests MUST NOT require live credentials or a real device
