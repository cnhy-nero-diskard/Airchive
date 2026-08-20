# Airchive

Headless telemetry collector for a single LG ThinQ air conditioner.

Every five minutes it reads the device's cumulative daily energy counter and its
full readable state through LG's **official** ThinQ Connect API, and persists
each observation as an immutable record in a dedicated Firestore project.

The official API exposes no instantaneous power property for air conditioners,
so the daily counter is the only energy signal there is — which is why the
collector samples it often and differences consecutive readings, classifying
every observation rather than ever emitting a plausible-looking wrong number.

No UI, no dashboard, no analytics layer. Inspection is done from a terminal.

```bash
airchive check-firestore     # prove storage connectivity
airchive discover            # find the device, its energy property, unit, precision
airchive validate-counter    # does the daily counter advance intraday?
airchive poll --once         # one cycle
airchive latest              # recent observations
airchive health              # collector health
```

## Documentation

- [docs/setup.md](docs/setup.md) — first-time setup: Firebase project, security
  rules, credentials, ThinQ token and client ID.
- [docs/operations.md](docs/operations.md) — configuration, deployment, the data
  model, interval semantics, the quality model, inspection, and known API
  limitations.
- [docs/discovery-findings.md](docs/discovery-findings.md) — what the real
  device does, recorded from `discover` and `validate-counter`.
