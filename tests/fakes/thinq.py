"""A controllable stand-in for `thinqconnect.ThinQApi`.

Each method returns whatever the test queued for it, or raises whatever the test
queued. Every call is recorded so tests can assert on the per-cycle call budget.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from thinqconnect import ThinQAPIException


def thinq_api_exception(
    code: str, message: str = "boom", token: str = "unused"
) -> ThinQAPIException:
    """Build the exception exactly as the SDK does — headers included.

    The SDK passes `self._generate_headers(...)`, so the real exception carries
    `Authorization: Bearer <PAT>`. Tests depend on that being reproduced faithfully.
    """
    return ThinQAPIException(
        code=code,
        message=message,
        headers={
            "Authorization": f"Bearer {token}",
            "x-country": "PH",
            "x-client-id": "test-client-id",
            "x-api-key": "test-api-key",
        },
    )


@dataclass
class RecordedCall:
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


@dataclass
class FakeThinqApi:
    """Implements the `ThinqApiLike` protocol with scripted responses."""

    device_list: Any = None
    device_profile: Any = None
    device_status: Any = None
    energy_profile: Any = None
    #: Either a value, an exception to raise, or a callable(device_id, prop, period, start, end).
    energy_usage: Any = None

    calls: list[RecordedCall] = field(default_factory=list)

    def calls_to(self, method: str) -> list[RecordedCall]:
        return [c for c in self.calls if c.method == method]

    def call_count(self, method: str) -> int:
        return len(self.calls_to(method))

    def _resolve(self, value: Any, *args: Any) -> Any:
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, list) and value and isinstance(value[0], _Scripted):
            step = value.pop(0)
            return self._resolve(step.value, *args)
        if callable(value) and not isinstance(value, dict | list | str | int | float):
            return self._resolve(value(*args), *args)
        return value

    def _record(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(RecordedCall(method, args, kwargs))

    async def async_get_device_list(self, timeout: int | float = 15) -> Any:
        self._record("async_get_device_list", timeout=timeout)
        return self._resolve(self.device_list)

    async def async_get_device_profile(self, device_id: str, timeout: int | float = 15) -> Any:
        self._record("async_get_device_profile", device_id, timeout=timeout)
        return self._resolve(self.device_profile, device_id)

    async def async_get_device_status(self, device_id: str, timeout: int | float = 15) -> Any:
        self._record("async_get_device_status", device_id, timeout=timeout)
        return self._resolve(self.device_status, device_id)

    async def async_get_device_energy_profile(
        self, device_id: str, timeout: int | float = 15
    ) -> Any:
        self._record("async_get_device_energy_profile", device_id, timeout=timeout)
        return self._resolve(self.energy_profile, device_id)

    async def async_get_device_energy_usage(
        self,
        device_id: str,
        energy_property: str,
        period: str,
        start_date: str,
        end_date: str,
        timeout: int | float = 15,
    ) -> Any:
        self._record(
            "async_get_device_energy_usage",
            device_id,
            energy_property,
            period,
            start_date,
            end_date,
            timeout=timeout,
        )
        return self._resolve(
            self.energy_usage, device_id, energy_property, period, start_date, end_date
        )


@dataclass
class _Scripted:
    value: Any


def sequence(*values: Any) -> list[_Scripted]:
    """Queue one response per call, in order."""
    return [_Scripted(v) for v in values]


def responder(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a callable as a dynamic responder (identity helper for readability)."""
    return fn
