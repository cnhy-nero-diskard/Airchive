## Purpose

Gives an operator everything needed to stand up, verify, and inspect the collector without a user interface: a guided and validated environment setup performed before the poller exists, read-only discovery and probing commands, and inspection commands that answer routine operational questions from the terminal.

## ADDED Requirements

### Requirement: Environment is established and validated before the poller is implemented

The telemetry storage environment SHALL be created and proven working end to end before collector polling logic is implemented, so that the first poller run is not the first test of connectivity.

#### Scenario: The operator is guided through environment creation

- **WHEN** the environment is first prepared
- **THEN** the operator SHALL be guided through creating a dedicated project for this system, enabling its document database, and choosing its region
- **AND** the guidance SHALL state explicitly that the database region is permanent once the database is created

#### Scenario: A connectivity test is performed

- **WHEN** the environment is prepared
- **THEN** a minimal write, read, and delete round trip against the database SHALL be performed from the local development environment
- **AND** the operator SHALL be able to confirm the result in the provider's console before the test record is removed

#### Scenario: Setup ordering is enforced

- **WHEN** the connectivity test has not yet succeeded
- **THEN** the collector's polling implementation SHALL NOT be treated as ready to build
- **AND** the outstanding setup step SHALL be reported to the operator

### Requirement: Local development credentials avoid unnecessary long-lived keys

Local development SHALL prefer ambient developer credentials. A long-lived credential file SHALL NOT be an unconditional requirement.

#### Scenario: Ambient developer credentials are available

- **WHEN** the developer has authenticated with the cloud provider's tooling
- **THEN** the collector SHALL use those ambient credentials
- **AND** it SHALL NOT require a credential file to be generated

#### Scenario: A credential file is genuinely required

- **WHEN** ambient credentials cannot be used and a credential file is necessary
- **THEN** it SHALL be stored outside the repository and referenced by environment configuration
- **AND** it MUST NOT be committed to version control
- **AND** the documentation SHALL explain how to revoke and replace it

#### Scenario: Ignore rules protect secrets

- **WHEN** the repository is initialized for this work
- **THEN** version-control ignore rules SHALL cover credential files and local environment files
- **AND** the committed configuration template SHALL contain no real secrets

### Requirement: Guided credential and identity setup for the device API

The operator SHALL be guided through obtaining the device API access token and establishing the stable client identity.

#### Scenario: The operator prepares API access

- **WHEN** the operator sets up device API access
- **THEN** the documentation SHALL describe how to obtain the personal access token, which country code applies, and how to generate the client identity once and persist it
- **AND** it SHALL state that the client identity is reused across runs rather than regenerated

### Requirement: Read-only discovery and validation commands

The collector SHALL provide operator commands that perform discovery and validate the energy counter's behavior without polling continuously and without controlling the device.

#### Scenario: Discovery is invoked

- **WHEN** the operator runs the discovery command
- **THEN** it SHALL report the registered devices, identify air-conditioner candidates with their identifier, alias, model name, and type, and report the device profile, the energy profile, the current state, and the current day's energy usage
- **AND** it SHALL indicate which energy property and unit should be configured

#### Scenario: Counter behavior is validated

- **WHEN** the operator runs the counter validation command over a period while the device is in normal use
- **THEN** it SHALL report the sequence of observed current-day values with their observation times, and summarize whether the value advances intraday, its apparent update latency, and its numeric precision
- **AND** it SHALL NOT issue any control command to the device

#### Scenario: Discovery output is safe to share

- **WHEN** discovery or validation output is produced
- **THEN** it SHALL contain no token, credential, or authorization header

### Requirement: Telemetry inspection commands

The collector SHALL provide commands that answer routine operational questions from the terminal, so that inspection does not require clicking through a console.

#### Scenario: Recent observations are listed

- **WHEN** the operator asks for the most recent observations
- **THEN** the command SHALL list them in reverse time order with their sample identifier, observation time, raw cumulative value, derived interval consumption and duration, interval status, and source outcomes

#### Scenario: The last successful cycle is identified

- **WHEN** the operator asks for the most recent successful observation
- **THEN** the command SHALL report it together with its storage path

#### Scenario: Health is reported

- **WHEN** the operator asks for collector health
- **THEN** the command SHALL report the current health record, including last attempt, last success, last error and its class, consecutive failures, and collector version

#### Scenario: Anomalous observations are found

- **WHEN** the operator asks for observations with problematic quality
- **THEN** the command SHALL return those whose interval status or condition markers indicate an anomaly, over a requested time range

#### Scenario: Stored state is compared against live state

- **WHEN** the operator asks to compare the latest stored observation with a fresh device reading
- **THEN** the command SHALL retrieve current state and energy from the API and present the differences against the stored record
- **AND** the comparison SHALL NOT write to the telemetry series

#### Scenario: Console inspection remains possible

- **WHEN** an operator prefers visual confirmation
- **THEN** the stored data SHALL remain legible in the provider's console
- **AND** the console SHALL be usable to confirm that telemetry is arriving

### Requirement: Operational documentation

The change SHALL be accompanied by documentation sufficient for an operator or a future agent to run and understand the system without rediscovering it.

#### Scenario: Documentation is complete

- **WHEN** the change is complete
- **THEN** the documentation SHALL cover project and database creation, local credential setup, API token and client identity setup, device discovery, every environment variable, local execution, deployed execution, the stored data model, the inspection commands, collector health, rate-limit behavior, interval and delta semantics, day-rollover reconciliation, and known device and API limitations

#### Scenario: Findings from discovery are recorded

- **WHEN** discovery and counter validation have been performed against the real device
- **THEN** their findings SHALL be recorded in the documentation, including the confirmed energy property, unit, precision, observed update latency, effective day boundary, and the readable properties this device actually exposes
