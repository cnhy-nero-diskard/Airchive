"""Failure classification (design D3; spec: thinq-connect-integration — failure taxonomy)."""

from __future__ import annotations

import json

import pytest
from aiohttp import ClientConnectionError, ContentTypeError, RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from airchive.thinq.failures import (
    FailureClass,
    ThinqFailure,
    classify_code,
    classify_exception,
)
from tests.fakes import thinq_api_exception


def _content_type_error() -> ContentTypeError:
    """The error raised when `await response.json()` meets an HTML gateway page.

    `async_request` calls `.json()` *before* checking `response.ok`, so a 502 that
    returns HTML surfaces as this, never as a `ThinQAPIException`.
    """
    request_info = RequestInfo(
        url=URL("https://api-kic.lgthinq.com/devices"),
        method="GET",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=URL("https://api-kic.lgthinq.com/devices"),
    )
    return ContentTypeError(request_info, ())


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("1306", FailureClass.RATE_LIMITED),  # EXCEEDED_API_CALLS
        ("1305", FailureClass.RATE_LIMITED),  # NOT_ALLOWED_API
        ("1309", FailureClass.RATE_LIMITED),  # NOT_ALLOWED_API_AGAIN
        ("1103", FailureClass.AUTH_FATAL),  # INVALID_TOKEN
        ("1218", FailureClass.AUTH_FATAL),  # INVALID_TOKEN_AGAIN
        ("1302", FailureClass.AUTH_FATAL),  # NOT_FOUND_TOKEN
        ("1222", FailureClass.DEVICE_OFFLINE),  # NOT_CONNECTED_DEVICE
        ("2000", FailureClass.TRANSIENT),  # INTERNAL_SERVER_ERROR
        ("2209", FailureClass.TRANSIENT),  # DEVICE_RESPONSE_DELAY
        ("2210", FailureClass.TRANSIENT),  # RETRY_REQUEST
        ("2212", FailureClass.TRANSIENT),  # SYNCING
        ("1219", FailureClass.CONFIG_FATAL),  # NOT_SUPPORTED_MODEL
        ("1220", FailureClass.CONFIG_FATAL),  # NOT_SUPPORTED_FEATURE
        ("1224", FailureClass.CONFIG_FATAL),  # INVALID_DEVICE_ID
        ("4242", FailureClass.UNKNOWN),  # unmapped
    ],
)
def test_error_codes_classify(code, expected):
    assert classify_code(code) is expected
    assert classify_exception(thinq_api_exception(code)).failure_class is expected


def test_non_json_body_is_malformed_not_an_api_error():
    failure = classify_exception(_content_type_error())

    assert failure.failure_class is FailureClass.MALFORMED
    assert failure.code is None


def test_json_decode_error_is_malformed():
    failure = classify_exception(json.JSONDecodeError("Expecting value", "<html>", 0))
    assert failure.failure_class is FailureClass.MALFORMED


def test_timeout_is_transport():
    assert classify_exception(TimeoutError()).failure_class is FailureClass.TRANSPORT
    assert classify_exception(TimeoutError()).failure_class is FailureClass.TRANSPORT


def test_connection_error_is_transport():
    failure = classify_exception(ClientConnectionError("cannot connect"))
    assert failure.failure_class is FailureClass.TRANSPORT


def test_unexpected_exception_is_unknown_and_says_nothing_about_it():
    failure = classify_exception(RuntimeError("some internal detail"))

    assert failure.failure_class is FailureClass.UNKNOWN
    assert "some internal detail" not in failure.safe_message


def test_retry_policy_follows_from_the_class():
    assert ThinqFailure(FailureClass.TRANSIENT).is_retryable
    assert ThinqFailure(FailureClass.TRANSPORT).is_retryable
    assert ThinqFailure(FailureClass.RATE_LIMITED).is_retryable

    assert ThinqFailure(FailureClass.AUTH_FATAL).is_fatal
    assert not ThinqFailure(FailureClass.AUTH_FATAL).is_retryable
    assert ThinqFailure(FailureClass.CONFIG_FATAL).is_fatal
    assert not ThinqFailure(FailureClass.CONFIG_FATAL).is_retryable

    # Device unavailability is neither a collector fault nor worth retrying hard.
    assert not ThinqFailure(FailureClass.DEVICE_OFFLINE).is_fatal
    assert not ThinqFailure(FailureClass.DEVICE_OFFLINE).is_retryable


def test_failure_keeps_code_and_error_name():
    failure = classify_exception(thinq_api_exception("1306", "too many calls"))

    assert failure.code == "1306"
    assert failure.error_name == "EXCEEDED_API_CALLS"
    assert failure.safe_message == "too many calls"
    assert failure.to_dict() == {
        "failureClass": "RATE_LIMITED",
        "code": "1306",
        "errorName": "EXCEEDED_API_CALLS",
        "message": "too many calls",
    }
