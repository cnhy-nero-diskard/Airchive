# Airchive

Headless telemetry collector for a single LG ThinQ air conditioner.

It samples the device's cumulative daily energy counter and its full readable
state roughly every five minutes through LG's **official** ThinQ Connect API,
and persists each observation as an immutable record in a dedicated Firestore
project.

No UI, no dashboard, no analytics layer. Inspection is done from the terminal.

See [docs/operations.md](docs/operations.md) for setup and operation.
