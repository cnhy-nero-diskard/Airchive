"""The PAT must not survive the SDK boundary (design D2).

`ThinQAPIException` is built with the outbound request headers, so it carries
`Authorization: Bearer <PAT>`. These tests use a sentinel token and assert it
appears in nothing that could ever be written down.
"""

from __future__ import annotations

import json
import logging
import traceback

import pytest

from airchive.redaction import clear_secrets, register_secret, scrub
from airchive.thinq.client import ThinqClient
from airchive.thinq.failures import FailureClass, ThinqRequestError
from tests.fakes import FakeThinqApi, thinq_api_exception

SENTINEL_PAT = "SENTINEL-PAT-9d41c7b0e6f24a1b8c53d7e9f0a1b2c3"


@pytest.fixture(autouse=True)
def _register_sentinel():
    register_secret(SENTINEL_PAT)
    yield
    clear_secrets()


def _raising_client(code: str = "1306", message: str = "quota exceeded") -> ThinqClient:
    exc = thinq_api_exception(code, message, token=SENTINEL_PAT)
    # Sanity check: the fixture reproduces the leak the boundary exists to stop.
    assert SENTINEL_PAT in json.dumps(exc.headers)
    return ThinqClient(FakeThinqApi(device_status=exc))


async def test_the_sdk_exception_never_escapes_the_boundary():
    client = _raising_client()

    with pytest.raises(ThinqRequestError) as excinfo:
        await client.get_device_status("device-1")

    error = excinfo.value
    assert error.failure.failure_class is FailureClass.RATE_LIMITED
    # No chained context: `raise ... from None` keeps the original out of tracebacks.
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not hasattr(error.failure, "headers")


async def test_the_token_is_absent_from_a_serialized_failure():
    client = _raising_client()

    with pytest.raises(ThinqRequestError) as excinfo:
        await client.get_device_status("device-1")

    failure = excinfo.value.failure
    assert SENTINEL_PAT not in json.dumps(failure.to_dict())
    assert SENTINEL_PAT not in str(failure)
    assert SENTINEL_PAT not in repr(failure)
    assert SENTINEL_PAT not in str(excinfo.value)


async def test_the_token_is_absent_from_a_rendered_log_record(caplog):
    client = _raising_client()
    logger = logging.getLogger("airchive.test")

    with caplog.at_level(logging.DEBUG):
        try:
            await client.get_device_status("device-1")
        except ThinqRequestError:
            # The most dangerous thing ordinary code does with an exception.
            logger.exception("cycle failed")

    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    rendered = "\n".join(formatter.format(record) for record in caplog.records)

    assert rendered  # the test would pass vacuously with no records
    assert SENTINEL_PAT not in rendered


async def test_the_token_is_absent_from_a_formatted_traceback():
    client = _raising_client()

    try:
        await client.get_device_status("device-1")
    except ThinqRequestError as exc:
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    assert SENTINEL_PAT not in rendered


async def test_transport_messages_are_scrubbed_before_they_are_kept():
    from aiohttp import ClientConnectionError

    api = FakeThinqApi(
        device_status=ClientConnectionError(f"failed with Bearer {SENTINEL_PAT}")
    )
    client = ThinqClient(api)

    with pytest.raises(ThinqRequestError) as excinfo:
        await client.get_device_status("device-1")

    assert SENTINEL_PAT not in excinfo.value.failure.safe_message
    assert "<redacted>" in excinfo.value.failure.safe_message


def test_scrub_leaves_ordinary_text_alone():
    assert scrub("nothing secret here") == "nothing secret here"
    assert scrub(f"token={SENTINEL_PAT}") == "token=<redacted>"


async def test_successful_calls_pass_the_payload_through_untouched():
    payload = {"response": "ignored", "operation": {"airConOperationMode": "POWER_ON"}}
    client = ThinqClient(FakeThinqApi(device_status=payload))

    assert await client.get_device_status("device-1") is payload


async def test_every_request_carries_an_explicit_timeout():
    api = FakeThinqApi(device_status={}, device_list=[], energy_profile={}, energy_usage={})
    client = ThinqClient(api, timeout_seconds=7.5)

    from datetime import date

    await client.get_device_status("d")
    await client.get_device_list()
    await client.get_energy_profile("d")
    await client.get_daily_energy_usage("d", "energyConsumption", date(2026, 8, 20))

    assert api.calls, "no calls recorded"
    for call in api.calls:
        assert call.kwargs["timeout"] == 7.5
