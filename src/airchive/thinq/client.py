"""The one place allowed to touch the ThinQ SDK (design D1, D2).

Two rules hold here and nowhere else:

1. **Nothing from the SDK escapes.** `ThinQAPIException` is constructed with the
   *outbound request* headers, which carry `Authorization: Bearer <PAT>`. Any
   handler, logger, or serializer that saw that object could exfiltrate the
   token. Every exception is converted to a `ThinqFailure` here and re-raised as
   `ThinqRequestError` with `from None`, so the original never reaches a
   traceback either.
2. **Only the low-level `ThinQApi` is used.** `ConnectBaseDevice` validates
   energy date ranges against `date.today()` — the process's *system-local* date
   — and raises for the first 8 hours of every Manila day under a UTC clock.
   The low-level call performs no such validation; property validation is ours,
   done at startup against the device's energy profile.

Only read operations are exposed. There is deliberately no control method.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Protocol

from airchive.config import ThinqConfig
from airchive.redaction import register_secret
from airchive.thinq.failures import ThinqRequestError, classify_exception

#: Period argument for the cumulative current-day energy counter.
PERIOD_DAILY = "DAILY"

#: ThinQ's energy-usage date format. Confirmed against the SDK's own
#: `_get_date_type_instance`, which parses an 8-digit all-numeric string.
DATE_FORMAT = "%Y%m%d"

DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0


class ThinqApiLike(Protocol):
    """The slice of `thinqconnect.ThinQApi` this collector uses. All read-only."""

    async def async_get_device_list(self, timeout: int | float = ...) -> Any: ...

    async def async_get_device_profile(
        self, device_id: str, timeout: int | float = ...
    ) -> Any: ...

    async def async_get_device_status(self, device_id: str, timeout: int | float = ...) -> Any: ...

    async def async_get_device_energy_profile(
        self, device_id: str, timeout: int | float = ...
    ) -> Any: ...

    async def async_get_device_energy_usage(
        self,
        device_id: str,
        energy_property: str,
        period: str,
        start_date: str,
        end_date: str,
        timeout: int | float = ...,
    ) -> Any: ...


class ThinqClient:
    """Sanitizing, read-only facade over the ThinQ Connect API."""

    def __init__(
        self,
        api: ThinqApiLike,
        *,
        timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ):
        self._api = api
        self._timeout = timeout_seconds

    async def _call(self, coro_factory) -> Any:
        try:
            return await coro_factory()
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # Shutdown, not a ThinQ failure. Let it propagate untouched.
            raise
        except BaseException as exc:  # noqa: BLE001 — this is the containment boundary
            failure = classify_exception(exc)
        # Raised *outside* the handler on purpose: raising inside it would leave the
        # original exception hanging off `__context__`, where a structured logger
        # walking `__dict__` would still find the bearer token.
        raise ThinqRequestError(failure) from None

    async def get_device_list(self) -> Any:
        return await self._call(lambda: self._api.async_get_device_list(timeout=self._timeout))

    async def get_device_profile(self, device_id: str) -> Any:
        return await self._call(
            lambda: self._api.async_get_device_profile(device_id, timeout=self._timeout)
        )

    async def get_device_status(self, device_id: str) -> Any:
        return await self._call(
            lambda: self._api.async_get_device_status(device_id, timeout=self._timeout)
        )

    async def get_energy_profile(self, device_id: str) -> Any:
        return await self._call(
            lambda: self._api.async_get_device_energy_profile(device_id, timeout=self._timeout)
        )

    async def get_daily_energy_usage(
        self,
        device_id: str,
        energy_property: str,
        day: date,
        *,
        end_day: date | None = None,
    ) -> Any:
        """Fetch the DAILY cumulative energy counter for `day`.

        `day` is the device's *local* day. No comparison against the process's
        system date happens anywhere in this path.
        """
        start = day.strftime(DATE_FORMAT)
        end = (end_day or day).strftime(DATE_FORMAT)
        return await self._call(
            lambda: self._api.async_get_device_energy_usage(
                device_id,
                energy_property,
                PERIOD_DAILY,
                start,
                end,
                timeout=self._timeout,
            )
        )


@asynccontextmanager
async def open_client(
    config: ThinqConfig,
    *,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
):
    """Build a `ThinqClient` backed by the real SDK, closing its session on exit."""
    import aiohttp
    from thinqconnect.thinq_api import ThinQApi

    register_secret(config.pat)

    session = aiohttp.ClientSession()
    try:
        api = ThinQApi(
            session=session,
            access_token=config.pat,
            country_code=config.country_code,
            client_id=config.client_id,
        )
        yield ThinqClient(api, timeout_seconds=timeout_seconds)
    finally:
        await session.close()
