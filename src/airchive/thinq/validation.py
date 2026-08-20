"""Startup validation of the configured energy property.

The device's own energy profile is the authority on which properties exist. The
list is read from cached metadata where available, so a routine cycle stays
inside its per-cycle call budget (spec: thinq-connect-integration).
"""

from __future__ import annotations

from airchive.config import ConfigError


def check_energy_property_supported(energy_property: str, supported: list[str]) -> None:
    """Fail startup when the configured property is not one the device exposes."""
    if energy_property in supported:
        return

    if supported:
        names = ", ".join(sorted(supported))
        detail = f"This device supports: {names}."
    else:
        detail = (
            "This device's energy profile lists no properties at all, so no energy "
            "polling is possible against it. Re-run `airchive discover` to confirm."
        )

    raise ConfigError(
        [
            f"LG_ENERGY_PROPERTY={energy_property!r} is not supported by device. {detail}",
        ]
    )
