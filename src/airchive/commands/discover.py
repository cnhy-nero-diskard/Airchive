"""`discover` — read-only inventory of what this ThinQ account actually exposes.

Answers the questions the rest of the configuration depends on: which device,
which energy property, which unit, what precision, and which state properties
this model really populates. Issues no control command.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from typing import Any

from airchive.config import ConfigError, load_discovery_config
from airchive.redaction import scrub_object
from airchive.thinq.client import open_client
from airchive.thinq.failures import ThinqRequestError
from airchive.thinq.payloads import (
    DeviceCandidate,
    extract_energy_reading,
    parse_device_list,
    supported_energy_properties,
)


def _dump(label: str, payload: Any) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(scrub_object(payload), indent=2, ensure_ascii=False, default=str))


def _heading(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


async def _report_device(client, device: DeviceCandidate, day, day_label: str) -> None:
    _heading(f"{device.alias or '(no alias)'} — {device.device_id}")
    print(f"deviceType: {device.device_type}")
    print(f"modelName:  {device.model_name}")

    try:
        profile = await client.get_device_profile(device.device_id)
        _dump("device profile (readable properties this model exposes)", profile)
    except ThinqRequestError as exc:
        print(f"\ndevice profile: FAILED — {exc.failure}")

    try:
        state = await client.get_device_status(device.device_id)
        _dump("current state", state)
    except ThinqRequestError as exc:
        print(f"\ncurrent state: FAILED — {exc.failure}")

    supported: list[str] = []
    try:
        energy_profile = await client.get_energy_profile(device.device_id)
        _dump("energy profile", energy_profile)
        supported = supported_energy_properties(energy_profile)
        print(f"\nsupported energy properties: {supported or '(none)'}")
    except ThinqRequestError as exc:
        print(f"\nenergy profile: FAILED — {exc.failure}")

    if not supported:
        print(
            "\nNo energy property is exposed for this device, so energy telemetry "
            "cannot be collected from it through the official API."
        )
        return

    print(f"\nToday ({day_label}, LG local day) usage per supported property:")
    for energy_property in supported:
        try:
            usage = await client.get_daily_energy_usage(device.device_id, energy_property, day)
        except ThinqRequestError as exc:
            print(f"  {energy_property}: FAILED — {exc.failure}")
            continue

        _dump(f"raw usage response — {energy_property}", usage)
        reading = extract_energy_reading(usage, energy_property, day_label=day_label)
        if reading is None:
            print(
                f"  {energy_property}: no numeric reading could be located in the "
                f"response above. Record its shape and reconcile the extractor."
            )
            continue
        print(
            f"  {energy_property}: value={reading.value} unit={reading.unit or '(unreported)'} "
            f"decimals={reading.decimal_places} dateLabel={reading.date_label}"
        )
        print(
            f"\n  Configure:\n"
            f"    LG_DEVICE_ID={device.device_id}\n"
            f"    LG_ENERGY_PROPERTY={energy_property}"
        )


async def _run(only_device_id: str | None) -> int:
    try:
        thinq, tzinfo, tz_name = load_discovery_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    now_local = datetime.now(tzinfo)
    day = now_local.date()
    day_label = day.isoformat()
    print(f"country: {thinq.country_code}   local day: {day_label} ({tz_name})")

    async with open_client(thinq) as client:
        try:
            payload = await client.get_device_list()
        except ThinqRequestError as exc:
            print(f"device list failed: {exc.failure}", file=sys.stderr)
            return 1

        devices = parse_device_list(payload)
        if not devices:
            print("No devices are registered to this account.", file=sys.stderr)
            _dump("raw device list", payload)
            return 1

        _heading("registered devices")
        for device in devices:
            marker = "AC" if device.is_air_conditioner else "  "
            print(
                f"[{marker}] {device.device_id}  alias={device.alias!r} "
                f"model={device.model_name!r} type={device.device_type!r}"
            )

        targets = [d for d in devices if d.is_air_conditioner]
        if only_device_id:
            targets = [d for d in devices if d.device_id == only_device_id]
            if not targets:
                print(f"\nNo device matches {only_device_id!r}.", file=sys.stderr)
                return 1
        if not targets:
            print(
                "\nNo air-conditioner devices found. Inspect the list above and "
                "re-run with --device-id to report on a specific device."
            )
            return 1

        for device in targets:
            await _report_device(client, device, day, day_label)

    print("\ndiscover: done (read-only; no control command was issued)")
    return 0


def run(device_id: str | None = None) -> int:
    return asyncio.run(_run(device_id))
