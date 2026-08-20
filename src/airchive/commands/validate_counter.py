"""`validate-counter` — Gate B.

The entire premise of sub-daily energy resolution is that LG's cumulative DAILY
counter advances *within* the day. The official API exposes no instantaneous
power property, so if the counter only moves once a day there is nothing to
sample. This command establishes which is true against the real device, and
records the update latency, precision, unit, and day boundary along the way.

Read-only: it issues no control command and writes nothing to Firestore.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from airchive.config import ConfigError, load_discovery_config
from airchive.thinq.client import open_client
from airchive.thinq.failures import ThinqRequestError
from airchive.thinq.payloads import extract_energy_reading


@dataclass
class Sample:
    observed_at: datetime
    local_day: str
    value: Decimal | None
    unit: str | None
    decimals: int
    note: str = ""


def _summarize(samples: list[Sample], interval_seconds: int) -> str:
    usable = [s for s in samples if s.value is not None]
    lines: list[str] = []

    lines.append(f"samples taken:        {len(samples)} ({len(usable)} with a usable value)")
    if not usable:
        lines.append("VERDICT: no usable readings were obtained. Gate B is unresolved.")
        return "\n".join(lines)

    units = {s.unit for s in usable if s.unit}
    unit_text = ", ".join(sorted(units)) if units else "(unreported)"
    lines.append(f"unit reported:        {unit_text}")
    lines.append(f"decimal places:       {max(s.decimals for s in usable)}")
    first, last = usable[0], usable[-1]
    lines.append(f"first value:          {first.value} at {first.observed_at.isoformat()}")
    lines.append(f"last value:           {last.value} at {last.observed_at.isoformat()}")

    days = sorted({s.local_day for s in usable})
    lines.append(f"local days covered:   {', '.join(days)}")

    # Changes within a single local day are what Gate B is about.
    changes: list[tuple[datetime, Decimal, Decimal]] = []
    decreases: list[tuple[datetime, Decimal, Decimal]] = []
    resets: list[tuple[datetime, Decimal, Decimal]] = []
    for previous, current in zip(usable, usable[1:], strict=False):
        if current.value == previous.value:
            continue
        if current.local_day != previous.local_day:
            resets.append((current.observed_at, previous.value, current.value))
            continue
        if current.value > previous.value:
            changes.append((current.observed_at, previous.value, current.value))
        else:
            decreases.append((current.observed_at, previous.value, current.value))

    same_day_advances = len(changes)
    lines.append(f"intraday increases:   {same_day_advances}")
    lines.append(f"intraday decreases:   {len(decreases)}  (retroactive provider revisions)")

    if changes:
        gaps = [
            (b[0] - a[0]).total_seconds()
            for a, b in zip(changes, changes[1:], strict=False)
        ]
        smallest = min(c[2] - c[1] for c in changes)
        lines.append(f"smallest increment:   {smallest}")
        if gaps:
            lines.append(
                f"update latency:       min {min(gaps):.0f}s / median "
                f"{sorted(gaps)[len(gaps) // 2]:.0f}s / max {max(gaps):.0f}s "
                f"between observed changes (sampled every {interval_seconds}s)"
            )
        else:
            lines.append(
                "update latency:       only one change observed; sample longer for a latency figure"
            )

    # Runs of identical values suggest caching or a coarse provider update cadence.
    longest_run = current_run = 1
    for previous, current in zip(usable, usable[1:], strict=False):
        current_run = current_run + 1 if current.value == previous.value else 1
        longest_run = max(longest_run, current_run)
    lines.append(
        f"longest unchanged run: {longest_run} samples "
        f"(~{longest_run * interval_seconds}s of an unchanged counter)"
    )

    if resets:
        for at, before, after in resets:
            lines.append(f"DAY RESET observed:   {before} -> {after} at {at.isoformat()}")
        lines.append(
            "Compare the reset time above against midnight in LG_DAY_TIMEZONE. "
            "A mismatch means the configured timezone is not LG's accounting day."
        )

    lines.append("")
    if same_day_advances > 0:
        lines.append(
            "VERDICT: the current-day counter DOES advance intraday. Gate B passes; "
            "sub-daily energy resolution is viable at this cadence."
        )
    else:
        lines.append(
            "VERDICT: no intraday advance was observed. Either the device consumed "
            "no power during the run, or LG updates this counter only once per day. "
            "Re-run while the air conditioner is actually running before treating "
            "Gate B as failed."
        )
    return "\n".join(lines)


async def _run(duration_minutes: int, interval_seconds: int) -> int:
    try:
        thinq, tzinfo, tz_name = load_discovery_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    missing = [
        name
        for name, value in (
            ("LG_DEVICE_ID", thinq.device_id),
            ("LG_ENERGY_PROPERTY", thinq.energy_property),
        )
        if not value
    ]
    if missing:
        print(
            f"validate-counter needs {' and '.join(missing)}. Run `airchive discover` first.",
            file=sys.stderr,
        )
        return 1

    deadline_seconds = duration_minutes * 60
    print(
        f"Sampling {thinq.energy_property} on {thinq.device_id} every {interval_seconds}s "
        f"for {duration_minutes} minutes ({tz_name}). Read-only. Ctrl-C to stop early.\n"
    )
    print(f"{'observed at':<26} {'local day':<12} {'value':>14}  unit")

    samples: list[Sample] = []
    started = datetime.now(tzinfo)

    async with open_client(thinq) as client:
        try:
            while True:
                now_local = datetime.now(tzinfo)
                day = now_local.date()
                note = ""
                reading = None
                try:
                    usage = await client.get_daily_energy_usage(
                        thinq.device_id, thinq.energy_property, day
                    )
                    reading = extract_energy_reading(
                        usage, thinq.energy_property, day_label=day.isoformat()
                    )
                    if reading is None:
                        note = "no numeric reading found in response"
                except ThinqRequestError as exc:
                    note = str(exc.failure)

                sample = Sample(
                    observed_at=now_local,
                    local_day=day.isoformat(),
                    value=reading.value if reading else None,
                    unit=reading.unit if reading else None,
                    decimals=reading.decimal_places if reading else 0,
                    note=note,
                )
                samples.append(sample)
                value_text = str(sample.value) if sample.value is not None else "—"
                print(
                    f"{sample.observed_at.isoformat(timespec='seconds'):<26} "
                    f"{sample.local_day:<12} {value_text:>14}  "
                    f"{sample.unit or ''}{('  ' + note) if note else ''}",
                    flush=True,
                )

                if (datetime.now(tzinfo) - started).total_seconds() >= deadline_seconds:
                    break
                await asyncio.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("\ninterrupted — summarizing what was collected")

    print(f"\n{'=' * 72}\nvalidate-counter summary\n{'=' * 72}")
    print(_summarize(samples, interval_seconds))
    print(
        "\nRecord these findings in docs/discovery-findings.md — the schema and the "
        "day-rollover behavior are reconciled against them."
    )
    return 0


def run(duration_minutes: int = 180, interval_seconds: int = 60) -> int:
    if interval_seconds < 1:
        print("--interval-seconds must be at least 1", file=sys.stderr)
        return 2
    return asyncio.run(_run(duration_minutes, interval_seconds))
