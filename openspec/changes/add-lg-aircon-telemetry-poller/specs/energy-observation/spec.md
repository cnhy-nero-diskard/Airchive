## Purpose

Defines the measurement semantics of a telemetry observation: what the raw cumulative energy counter means, how interval consumption is derived from consecutive readings, how day boundaries and anomalies are handled, and how every observation's trustworthiness is expressed so that no derived number is ever silently wrong.

## ADDED Requirements

### Requirement: Raw source values are authoritative

Every observation SHALL preserve the unmodified current-day cumulative energy value obtained from the API. Derived values SHALL NOT be the only energy information stored.

#### Scenario: An observation is recorded

- **WHEN** an energy reading is obtained successfully
- **THEN** the observation SHALL contain the raw current-day cumulative value exactly as reported, together with its unit
- **AND** it SHALL be possible to recompute every derived energy field from the stored raw values of this and prior observations

#### Scenario: A derived value cannot be computed

- **WHEN** interval consumption cannot be determined for any reason
- **THEN** the raw current-day value SHALL still be stored
- **AND** the observation SHALL NOT be discarded merely because a derived field is unavailable

### Requirement: Interval consumption for two readings in the same local day

When the current reading and the previous usable reading belong to the same LG local day, interval consumption SHALL be the difference between the current and previous raw cumulative values.

#### Scenario: Normal same-day increment

- **WHEN** the previous reading was 2.100 and the current reading is 2.150 on the same local day
- **THEN** interval consumption SHALL be 0.050
- **AND** the interval status SHALL be recorded as normal

#### Scenario: The counter has not advanced

- **WHEN** the previous reading was 2.100 and the current reading is 2.100 on the same local day
- **THEN** interval consumption SHALL be 0
- **AND** the observation SHALL be retained rather than discarded
- **AND** the observation SHALL be marked as showing an unchanged counter, so a later analysis can distinguish genuine idleness from provider update latency

#### Scenario: Arithmetic does not introduce spurious precision

- **WHEN** a difference is computed between two decimal readings
- **THEN** the result SHALL be rounded to the precision of the source counter as established during validation
- **AND** binary floating-point representation artifacts MUST NOT appear in stored values

### Requirement: Elapsed interval duration reflects actual observation times

Interval duration SHALL be derived from the actual observation timestamps of the two readings, not from the configured polling cadence.

#### Scenario: Readings are separated by more than one cadence

- **WHEN** the previous usable reading was observed roughly 15 minutes before the current reading because intervening cycles failed
- **THEN** the recorded interval duration SHALL be approximately 900 seconds
- **AND** it MUST NOT be recorded as the nominal cadence

#### Scenario: Missed cycles produce a coarser but valid interval

- **WHEN** readings at 12:00 and 12:15 are 2.100 and 2.190 with failed cycles between them, both on the same local day
- **THEN** interval consumption SHALL be 0.090 over approximately 900 seconds
- **AND** the observation SHALL be marked as spanning a coarser interval than the nominal cadence
- **AND** it SHALL NOT be treated as invalid data

### Requirement: First observation establishes a baseline without inventing consumption

When no previous comparable reading exists, the accumulated day total MUST NOT be presented as consumption during the interval.

#### Scenario: No previous reading has ever been recorded for the device

- **WHEN** the current reading is 2.100 and no prior observation exists for this device
- **THEN** interval consumption SHALL be null
- **AND** the interval status SHALL indicate a new baseline
- **AND** the raw current-day value SHALL still be stored

#### Scenario: A previous reading exists but is not usable

- **WHEN** a prior observation exists but carries no usable raw energy value
- **THEN** interval consumption SHALL be null
- **AND** the interval status SHALL indicate a missing previous sample, distinct from a new baseline

### Requirement: Day rollover is never treated as an ordinary difference

When the current reading belongs to a later local day than the previous reading, the collector MUST NOT subtract the previous day's cumulative counter from the new day's counter.

#### Scenario: Naive subtraction would produce negative consumption

- **WHEN** the previous reading was 8.732 late in one local day and the current reading is 0.021 early in the next local day
- **THEN** the collector SHALL NOT record interval consumption of -8.711
- **AND** the observation SHALL be classified as a day rollover

#### Scenario: Cross-midnight interval is reconstructed from the finalized previous day

- **WHEN** the previous reading was 8.732, the finalized total for that previous local day is 8.751, and the current new-day reading is 0.021
- **THEN** interval consumption SHALL be 0.040, being the unobserved remainder of the previous day plus the new day's accumulation
- **AND** the interval status SHALL indicate a resolved day rollover
- **AND** the finalized previous-day total used SHALL be stored on the observation

#### Scenario: The finalized previous-day total is not yet available

- **WHEN** a day rollover occurs and the previous day's finalized total cannot be retrieved
- **THEN** interval consumption SHALL be null
- **AND** the interval status SHALL indicate an unresolved day rollover
- **AND** the observation SHALL be eligible for later reconciliation

#### Scenario: The finalized previous-day total is implausible

- **WHEN** the retrieved finalized previous-day total is less than the last observed reading for that day, or is otherwise not a sane value
- **THEN** the reconstruction SHALL NOT be performed
- **AND** interval consumption SHALL be null with an unresolved day rollover status
- **AND** the implausible value SHALL be recorded for later inspection

#### Scenario: More than one local day has elapsed since the previous reading

- **WHEN** the previous usable reading is older than the immediately preceding local day
- **THEN** the collector SHALL NOT attempt a cross-midnight reconstruction spanning multiple day boundaries
- **AND** interval consumption SHALL be null with a status indicating the gap
- **AND** the raw current-day value SHALL still be stored

### Requirement: Deferred reconciliation of unresolved rollovers

An observation left with an unresolved day rollover SHALL be reconcilable later, without altering its raw recorded values.

#### Scenario: The finalized total becomes available later

- **WHEN** a subsequent cycle retrieves a finalized total for a day that has an unresolved rollover observation
- **THEN** that observation's derived interval consumption MAY be filled in
- **AND** the observation SHALL be marked as reconciled, recording when reconciliation occurred
- **AND** the originally stored raw values MUST NOT be modified

#### Scenario: Reconciliation is bounded

- **WHEN** a finalized previous-day total remains unavailable beyond a defined reconciliation window
- **THEN** the collector SHALL stop retrying reconciliation for that observation
- **AND** the observation SHALL remain permanently marked as unresolved rather than being assigned a fabricated value

### Requirement: Same-day decrease never yields negative consumption

If the current raw value is lower than the previous raw value within the same local day, the collector MUST NOT record negative consumption.

#### Scenario: The counter decreases within a day

- **WHEN** the previous reading was 3.500 and the current reading is 3.200 on the same local day
- **THEN** interval consumption SHALL be null
- **AND** the interval status SHALL indicate an anomalous decrease
- **AND** both the current raw value and the previous raw value SHALL be stored

#### Scenario: A provider correction is retroactive

- **WHEN** a current-day value is observed to be lower than an earlier observation of the same day because the provider revised it
- **THEN** the earlier observation MUST NOT be rewritten
- **AND** the new observation SHALL be stored with the anomalous-decrease classification so the revision is visible in the historical series

### Requirement: Energy and state are independent observations

Energy retrieval and state retrieval SHALL be treated as related but independent. A failure of one MUST NOT discard the useful result of the other, and missing values MUST NOT be fabricated.

#### Scenario: Energy succeeds and state fails

- **WHEN** the energy reading is obtained but the device state cannot be retrieved
- **THEN** the observation SHALL be persisted with its energy fields populated and its state fields absent
- **AND** the source status SHALL record energy as successful and state as failed
- **AND** state values MUST NOT be carried forward from a prior observation or otherwise invented

#### Scenario: State succeeds and energy fails

- **WHEN** the device state is obtained but the energy reading cannot be retrieved
- **THEN** the observation SHALL be persisted with its state fields populated and its energy values absent
- **AND** the source status SHALL record state as successful and energy as failed
- **AND** the observation SHALL NOT be used as the previous energy reading for a later interval calculation

#### Scenario: Both fail

- **WHEN** neither the energy reading nor the device state can be retrieved
- **THEN** no telemetry values SHALL be fabricated
- **AND** the failure SHALL be recorded in collector health and in the logs for that cycle

### Requirement: Explicit, non-overloaded data quality model

Each observation SHALL carry an explicit quality representation. A single field MUST NOT be overloaded to express independent conditions: the reason a derived interval has its value, the outcome of each data source, and additional independent condition markers SHALL be represented separately.

#### Scenario: Interval status is recorded

- **WHEN** an observation is classified
- **THEN** exactly one interval status SHALL be recorded, drawn from a closed set that includes at minimum: normal, new baseline, missing previous sample, coarse interval, resolved day rollover, unresolved day rollover, anomalous decrease, and energy unavailable

#### Scenario: Source outcomes are recorded separately from interval status

- **WHEN** an observation is classified
- **THEN** the outcome of the energy request and the outcome of the state request SHALL each be recorded independently, including the failure class where applicable
- **AND** those outcomes MUST NOT be encoded into the interval status field

#### Scenario: Independent conditions are recorded as separate markers

- **WHEN** conditions such as rate limiting, device unavailability, an unchanged counter, a partial observation, or a completed reconciliation apply to an observation
- **THEN** each SHALL be recorded as an independent marker that can coexist with the others
- **AND** it SHALL be possible to query observations by any single marker

#### Scenario: Two independent conditions apply at once

- **WHEN** an observation is both a coarse interval and rate limited
- **THEN** both conditions SHALL be discoverable from the stored record
- **AND** neither SHALL displace the other

### Requirement: Rich device state is preserved as exposed

The observation SHALL preserve the readable device state properties that the target device actually exposes, without narrowing the record to a fixed minimal set and without inventing unavailable properties.

#### Scenario: The device exposes a property

- **WHEN** the device's profile and state expose a readable property useful for later analysis, including operating and power state, job mode, target and current temperature with unit, fan or wind strength, swing and wind direction, power-saving mode, air-clean mode, sleep and timer state, humidity, air-quality readings, and filter information
- **THEN** the observation SHALL record that property's value

#### Scenario: The device does not expose a property

- **WHEN** a property is absent from this device's profile or state
- **THEN** the observation SHALL omit it
- **AND** a placeholder or guessed value MUST NOT be stored

#### Scenario: Temperature unit is preserved

- **WHEN** temperature values are recorded
- **THEN** the unit reported by the device SHALL be recorded alongside them
- **AND** a unit MUST NOT be assumed when the device reports one
