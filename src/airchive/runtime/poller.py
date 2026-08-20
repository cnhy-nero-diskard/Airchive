"""Driving cycles on a cadence, and stopping cleanly.

The deployed topology runs one cycle per scheduled invocation (`--once`), so
this loop mainly serves local runs and long-lived hosts. Either way the cycle
itself is what carries the correctness; the loop only decides when to start one
and when to stop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from airchive.logging_setup import CONTEXT_KEY, get_logger
from airchive.runtime.cycle import CycleResult, CycleSettings, run_cycle
from airchive.storage.ids import floor_to_slot
from airchive.storage.store import TelemetryStore
from airchive.thinq.client import ThinqClient

logger = get_logger("poller")


def seconds_until_next_slot(now: datetime, interval_seconds: int) -> float:
    """Time from `now` to the start of the next slot."""
    next_slot = floor_to_slot(now, interval_seconds).timestamp() + interval_seconds
    return max(0.0, next_slot - now.timestamp())


async def run_once(
    *,
    client: ThinqClient,
    store: TelemetryStore,
    settings: CycleSettings,
    now: datetime | None = None,
    shutdown: asyncio.Event | None = None,
) -> CycleResult:
    return await run_cycle(
        client=client, store=store, settings=settings, now=now, shutdown=shutdown
    )


async def run_loop(
    *,
    client: ThinqClient,
    store: TelemetryStore,
    settings: CycleSettings,
    shutdown: asyncio.Event | None = None,
    max_cycles: int | None = None,
    clock: Callable[[], datetime] | None = None,
    wait: Callable[[float], Awaitable[None]] | None = None,
) -> list[CycleResult]:
    """Run cycles until shutdown is requested or `max_cycles` have run.

    A shutdown signal is honored *between* cycles, and inside a cycle only
    before its write. The observation for the slot in progress is therefore
    either written whole or not at all — there is no half-written record to
    clean up afterwards.
    """
    shutdown = shutdown or asyncio.Event()
    clock = clock or (lambda: datetime.now(UTC))
    results: list[CycleResult] = []

    async def default_wait(seconds: float) -> None:
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=seconds)
        except TimeoutError:
            pass

    wait = wait or default_wait

    while not shutdown.is_set():
        result = await run_cycle(
            client=client, store=store, settings=settings, now=clock(), shutdown=shutdown
        )
        results.append(result)

        if max_cycles is not None and len(results) >= max_cycles:
            break
        if shutdown.is_set():
            break

        await wait(seconds_until_next_slot(clock(), settings.interval_seconds))

    logger.info(
        "poller stopped",
        extra={CONTEXT_KEY: {"cycles": len(results), "shutdown": shutdown.is_set()}},
    )
    return results


def install_signal_handlers(shutdown: asyncio.Event) -> None:
    """Ask for a graceful stop on SIGINT/SIGTERM where the platform allows it."""
    import signal

    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        signal_number = getattr(signal, name, None)
        if signal_number is None:
            continue
        try:
            loop.add_signal_handler(signal_number, shutdown.set)
        except (NotImplementedError, RuntimeError):
            # Windows event loops do not support add_signal_handler; the
            # KeyboardInterrupt path in the command covers the interactive case.
            signal.signal(signal_number, lambda *_: shutdown.set())
