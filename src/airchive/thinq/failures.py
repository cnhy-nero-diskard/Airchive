"""ThinQ failure taxonomy (design D3).

HTTP status and `Retry-After` are not observable through the SDK — `async_request`
discards the `ClientResponse` — so classification keys on ThinQ's own error codes,
plus the exception type for transport and parse failures.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import StrEnum

from airchive.redaction import scrub


class FailureClass(StrEnum):
    """How the collector must respond to a failure."""

    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FATAL = "AUTH_FATAL"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    TRANSIENT = "TRANSIENT"
    CONFIG_FATAL = "CONFIG_FATAL"
    MALFORMED = "MALFORMED"
    TRANSPORT = "TRANSPORT"
    UNKNOWN = "UNKNOWN"


#: Classes that must never consume a retry attempt.
FATAL_CLASSES = frozenset({FailureClass.AUTH_FATAL, FailureClass.CONFIG_FATAL})

#: Classes eligible for bounded retry inside a cycle.
RETRYABLE_CLASSES = frozenset(
    {FailureClass.TRANSIENT, FailureClass.TRANSPORT, FailureClass.RATE_LIMITED}
)

#: ThinQ error code -> failure class. Codes absent here classify as UNKNOWN.
ERROR_CODE_CLASSES: dict[str, FailureClass] = {
    # Rate limiting / call not currently permitted.
    "1305": FailureClass.RATE_LIMITED,  # NOT_ALLOWED_API
    "1306": FailureClass.RATE_LIMITED,  # EXCEEDED_API_CALLS
    "1309": FailureClass.RATE_LIMITED,  # NOT_ALLOWED_API_AGAIN
    # Credentials: fatal, never retried.
    "1103": FailureClass.AUTH_FATAL,  # INVALID_TOKEN
    "1218": FailureClass.AUTH_FATAL,  # INVALID_TOKEN_AGAIN
    "1301": FailureClass.AUTH_FATAL,  # INVALID_SERVICE_KEY
    "1302": FailureClass.AUTH_FATAL,  # NOT_FOUND_TOKEN
    # The device, not the collector, is unavailable.
    "1222": FailureClass.DEVICE_OFFLINE,  # NOT_CONNECTED_DEVICE
    # Transient server-side conditions.
    "2000": FailureClass.TRANSIENT,  # INTERNAL_SERVER_ERROR
    "2209": FailureClass.TRANSIENT,  # DEVICE_RESPONSE_DELAY
    "2210": FailureClass.TRANSIENT,  # RETRY_REQUEST
    "2212": FailureClass.TRANSIENT,  # SYNCING
    "2214": FailureClass.TRANSIENT,  # FAIL_REQUEST
    # Configuration is wrong; retrying cannot fix it.
    "1205": FailureClass.CONFIG_FATAL,  # NOT_EXIST_DEVICE
    "1212": FailureClass.CONFIG_FATAL,  # NOT_OWNED_DEVICE
    "1213": FailureClass.CONFIG_FATAL,  # NOT_REGISTERED_DEVICE
    "1219": FailureClass.CONFIG_FATAL,  # NOT_SUPPORTED_MODEL
    "1220": FailureClass.CONFIG_FATAL,  # NOT_SUPPORTED_FEATURE
    "1221": FailureClass.CONFIG_FATAL,  # NOT_SUPPORTED_PRODUCT
    "1224": FailureClass.CONFIG_FATAL,  # INVALID_DEVICE_ID
    "1307": FailureClass.CONFIG_FATAL,  # NOT_SUPPORTED_COUNTRY
}


@dataclass(frozen=True)
class ThinqFailure:
    """A sanitized failure. Carries no request headers, no token, no SDK object."""

    failure_class: FailureClass
    code: str | None = None
    error_name: str | None = None
    safe_message: str = ""

    @property
    def is_fatal(self) -> bool:
        return self.failure_class in FATAL_CLASSES

    @property
    def is_retryable(self) -> bool:
        return self.failure_class in RETRYABLE_CLASSES

    def to_dict(self) -> dict[str, str | None]:
        return {
            "failureClass": str(self.failure_class),
            "code": self.code,
            "errorName": self.error_name,
            "message": self.safe_message,
        }

    def __str__(self) -> str:
        parts = [str(self.failure_class)]
        if self.code:
            parts.append(f"code={self.code}")
        if self.error_name:
            parts.append(self.error_name)
        if self.safe_message:
            parts.append(self.safe_message)
        return " ".join(parts)


class ThinqRequestError(Exception):
    """Internal error raised by the SDK boundary. Wraps only sanitized data."""

    def __init__(self, failure: ThinqFailure):
        self.failure = failure
        super().__init__(str(failure))


def classify_code(code: str | None) -> FailureClass:
    """Map a ThinQ error code to its failure class."""
    if code is None:
        return FailureClass.UNKNOWN
    return ERROR_CODE_CLASSES.get(str(code).strip(), FailureClass.UNKNOWN)


def classify_exception(exc: BaseException) -> ThinqFailure:
    """Convert an exception raised beneath the SDK boundary into a sanitized failure.

    Nothing from `exc` reaches the result except its type name and, where safe, a
    scrubbed message. In particular `ThinQAPIException.headers` — which holds
    `Authorization: Bearer <PAT>` — is never read.
    """
    # Imported lazily so the taxonomy stays importable without the SDK present.
    from aiohttp import ClientError, ContentTypeError
    from thinqconnect import ThinQAPIException

    if isinstance(exc, ThinQAPIException):
        code = str(getattr(exc, "code", "") or "") or None
        error_name = getattr(exc, "error_name", None)
        message = str(getattr(exc, "message", "") or "")
        return ThinqFailure(
            failure_class=classify_code(code),
            code=code,
            error_name=error_name,
            safe_message=scrub(message),
        )

    if isinstance(exc, ContentTypeError | json.JSONDecodeError):
        return ThinqFailure(
            failure_class=FailureClass.MALFORMED,
            error_name=type(exc).__name__,
            safe_message="response body was not valid JSON",
        )

    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return ThinqFailure(
            failure_class=FailureClass.TRANSPORT,
            error_name=type(exc).__name__,
            safe_message="request timed out",
        )

    if isinstance(exc, ClientError):
        return ThinqFailure(
            failure_class=FailureClass.TRANSPORT,
            error_name=type(exc).__name__,
            safe_message=scrub(str(exc)),
        )

    if isinstance(exc, KeyError | TypeError | ValueError):
        return ThinqFailure(
            failure_class=FailureClass.MALFORMED,
            error_name=type(exc).__name__,
            safe_message="response did not have the expected structure",
        )

    return ThinqFailure(
        failure_class=FailureClass.UNKNOWN,
        error_name=type(exc).__name__,
        safe_message="unexpected error at the ThinQ boundary",
    )


def malformed(detail: str) -> ThinqFailure:
    """A failure for a structurally wrong but successfully transported response."""
    return ThinqFailure(
        failure_class=FailureClass.MALFORMED,
        error_name="MalformedResponse",
        safe_message=scrub(detail),
    )
