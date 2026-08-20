"""`poll` — the collector itself.

`--once` runs a single cycle and exits, which is the shape the deployed Cloud
Run Job uses: one invocation per slot, all state reconstructed from Firestore.
Without it, the process stays up and runs a cycle per interval.
"""

from __future__ import annotations

import asyncio
import sys

from airchive import __version__
from airchive.config import ConfigError, load_config
from airchive.logging_setup import configure_logging
from airchive.redaction import register_secret
from airchive.runtime.cycle import CycleSettings
from airchive.runtime.poller import install_signal_handlers, run_loop, run_once
from airchive.storage.client import build_client
from airchive.storage.store import TelemetryStore
from airchive.thinq.client import open_client


async def _run(once: bool) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    configure_logging(config.log_level)
    register_secret(config.thinq.pat)

    settings = CycleSettings.from_config(config, collector_version=__version__)
    store = TelemetryStore(build_client(config.firestore.project_id), config.thinq.device_id)

    shutdown = asyncio.Event()
    install_signal_handlers(shutdown)

    async with open_client(config.thinq) as client:
        if once:
            result = await run_once(
                client=client, store=store, settings=settings, shutdown=shutdown
            )
            return 0 if (result.wrote or result.skipped == "lease") else 1

        try:
            await run_loop(
                client=client, store=store, settings=settings, shutdown=shutdown
            )
        except KeyboardInterrupt:
            shutdown.set()
    return 0


def run(once: bool = False) -> int:
    try:
        return asyncio.run(_run(once))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
