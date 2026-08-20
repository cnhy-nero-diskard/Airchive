# Discovery findings

What the **actual device** does, as opposed to what the API documentation and
the SDK source imply. Three assumptions in the design are load-bearing and can
only be settled by observation; this file is where the answers live.

> **Status: not yet recorded.** Run `airchive discover` and
> `airchive validate-counter` against the real device and fill this in. Until
> then, everything below is a placeholder and the collector is running on
> assumptions.

Fill it in by running:

```bash
airchive discover                                    # profiles, property, unit, precision
airchive validate-counter --duration-minutes 240     # counter behavior over hours of use
```

Both are read-only and issue no control command.

---

## 1. Device identity

| Field | Value |
|---|---|
| `deviceId` | _(from `discover`)_ |
| Alias | |
| Model name | |
| Device type | `DEVICE_AIR_CONDITIONER` |
| Recorded on | |

---

## 2. Energy property — Gate for `LG_ENERGY_PROPERTY`

| Question | Answer |
|---|---|
| Properties in `energy_profile["result"]["property"]` | |
| Property configured | |
| Unit reported by the API | |
| Decimal places observed | |
| Response shape (paste one raw response) | |

```json
(paste the raw energy-usage response here — the extractor is written
defensively against an unknown shape, and this is what confirms it)
```

The energy extractor searches for a numeric reading under the names ThinQ
plausibly uses and returns nothing rather than a guess. If it reported "no
numeric reading found" during discovery, record the real shape above and
reconcile `airchive/thinq/payloads.py` against it.

---

## 3. Gate B — does the current-day counter advance intraday?

**This is the assumption the entire project rests on.** The official API exposes
no instantaneous power property, so if LG only updates the daily counter once a
day, sub-daily energy resolution is impossible through a supported source and
the collector's value narrows to state-only history.

| Question | Answer |
|---|---|
| Does the value change within the day? | **_(yes / no)_** |
| Observation window (start → end, hours) | |
| Number of intraday increases observed | |
| Smallest increment seen | |
| Apparent update latency (min / median / max between changes) | |
| Longest run of an unchanged value | |
| Evidence of cached or repeated values | |
| Retroactive downward revisions of an already-observed value | |

**Verdict:**

- [ ] **Passes** — the counter advances intraday; five-minute sampling is
      meaningful.
- [ ] **Fails** — revisit the proposal before relying on energy deltas.
      State-only telemetry may still proceed.

Paste the `validate-counter` summary output here:

```
(paste)
```

---

## 4. The LG day boundary — Gate for `LG_DAY_TIMEZONE`

The daily bucket resets on a timezone owned by LG and the device, which may not
be the collector's configured one. A wrong boundary silently corrupts every
day-rollover reconstruction.

| Question | Answer |
|---|---|
| Observed reset time (local clock) | |
| Configured `LG_DAY_TIMEZONE` | `Asia/Manila` |
| Do they agree? | |
| Any timezone the API itself exposes for the device or account | |

If they disagree, that is a finding that affects rollover correctness — record
it here and change `LG_DAY_TIMEZONE` to match what was observed rather than
assuming the configured value governs.

---

## 5. Readable state properties this model actually exposes

The SDK defines twelve AC resource groups. This device populates some subset of
them. The collector stores exactly what is returned and invents nothing, so this
list is what the historical series will actually contain.

| Resource group | Present? | Properties observed |
|---|---|---|
| `operation` | | |
| `airConJobMode` | | |
| `temperatureInUnits` / `temperature` | | |
| `twoSetTemperature` | | |
| `airFlow` | | |
| `windDirection` | | |
| `powerSave` | | |
| `airQualitySensor` | | |
| `filterInfo` | | |
| `timer` | | |
| `sleepTimer` | | |
| `display` | | |

Open questions to settle here:

- Does this model expose `powerSaveEnabled` as a boolean only, or some
  percentage-style energy-control value? (The proposal's illustrative
  `energyControl: 40` does not appear in the SDK's AC property vocabulary and
  may not exist on this device.)
- Which temperature unit does it report, and does it ever change?

Paste the full `discover` state output here:

```json
(paste)
```

---

## 6. Rate limits

| Question | Answer |
|---|---|
| Published per-token call quota | |
| Headroom above ~576 calls/day for one device | |
| Any `1306 EXCEEDED_API_CALLS` seen during validation | |

---

## 7. Reconciliation against the provisional schema

Once the sections above are filled in, check each against what the code assumes:

- [ ] `LG_ENERGY_PROPERTY` matches a property the device really exposes.
- [ ] The stored `unit` matches what the API reports (nothing is assumed).
- [ ] Interval quantization matches the observed decimal precision.
- [ ] `LG_DAY_TIMEZONE` matches the observed reset boundary.
- [ ] The energy extractor handles the real response shape.
- [ ] The documented limitations in [operations.md](operations.md) match what
      was observed here.
