"""Bounded retries, backoff, and the rate-limit cooldown.

Three rules, each with a specific failure it prevents:

* **Fatal conditions consume no attempts.** Retrying a revoked token with
  backoff produces an infinitely quiet failure — exactly the silent data loss
  the health record exists to make visible.
* **Every retry is bounded in count *and* in total time.** A cycle that spends
  longer than its interval retrying starts overlapping the next one.
* **Delays carry jitter.** Several hundred scheduled invocations a day that all
  back off by the same amount converge into a synchronized burst.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from airchive.thinq.failures import FailureClass, ThinqFailure, ThinqRequestError

#: Failures worth another attempt inside the same cycle.
_RETRY_WITHIN_CYCLE = frozenset(
    {FailureClass.TRANSIENT, FailureClass.TRANSPORT, FailureClass.RATE_LIMITED}
)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounds for one request's attempts."""

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 15.0
    #: Rate limiting deserves a longer first wait than an ordinary blip.
    rate_limit_base_delay_seconds: float = 5.0
    #: Fraction of each delay applied as random jitter.
    jitter: float = 0.5
    #: Ceiling on the total time all attempts for one request may take.
    total_budget_seconds: float = 45.0

    def delay_for(self, attempt: int, failure_class: FailureClass, roll: float) -> float:
        """Exponential backoff with jitter for a 1-based attempt number."""
        base = (
            self.rate_limit_base_delay_seconds
            if failure_class is FailureClass.RATE_LIMITED
            else self.base_delay_seconds
        )
        delay = min(base * (2 ** (attempt - 1)), self.max_delay_seconds)
        return delay * (1 - self.jitter + self.jitter * 2 * roll)


#: A frozen, shared default so callers need not construct one.
DEFAULT_RETRY_POLICY = RetryPolicy()


@dataclass
class AttemptLog:
    """What actually happened, so a test can assert no tight loop occurred."""

    attempts: int = 0
    delays: list[float] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.delays is None:
            self.delays = []


async def call_with_retries[T](
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy | None = None,
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    monotonic: Callable[[], float] | None = None,
    roll: Callable[[], float] | None = None,
    log: AttemptLog | None = None,
) -> T:
    """Run `operation`, retrying only what is worth retrying.

    Raises the last `ThinqRequestError` when the attempts or the time budget run
    out. Clock, sleep, and randomness are injectable so tests never actually
    wait.
    """
    policy = policy or DEFAULT_RETRY_POLICY
    sleep = sleep or asyncio.sleep
    monotonic = monotonic or asyncio.get_event_loop().time
    roll = roll or random.random
    log = log if log is not None else AttemptLog()

    started = monotonic()
    last_error: ThinqRequestError | None = None

    for attempt in range(1, policy.max_attempts + 1):
        log.attempts = attempt
        try:
            return await operation()
        except ThinqRequestError as error:
            last_error = error
            failure = error.failure

            if failure.failure_class not in _RETRY_WITHIN_CYCLE:
                # Fatal, offline, or malformed: another attempt cannot help, and
                # for fatal conditions it would hide the problem.
                raise

            if attempt >= policy.max_attempts:
                raise

            delay = policy.delay_for(attempt, failure.failure_class, roll())
            elapsed = monotonic() - started
            if elapsed + delay > policy.total_budget_seconds:
                raise

            log.delays.append(delay)
            await sleep(delay)

    # Unreachable: the loop either returns or raises.
    raise last_error if last_error else RuntimeError("no attempt was made")


def rate_limit_cooldown_seconds(
    consecutive: int,
    *,
    base: float = 600.0,
    maximum: float = 3600.0,
    roll: float = 0.5,
    jitter: float = 0.5,
) -> float:
    """How long to stop calling ThinQ after sustained rate limiting.

    Applied *across* cycles, not within one: a cycle that starts inside the
    cooldown issues no requests at all, which is what actually reduces the
    effective request rate rather than merely re-pacing it.
    """
    if consecutive <= 0:
        return 0.0
    delay = min(base * (2 ** (consecutive - 1)), maximum)
    return delay * (1 - jitter + jitter * 2 * roll)


def failure_of(error: BaseException) -> ThinqFailure | None:
    return error.failure if isinstance(error, ThinqRequestError) else None


def is_fatal(error: BaseException) -> bool:
    failure = failure_of(error)
    return bool(failure and failure.is_fatal)


def describe(value: Any) -> str:
    return str(value) if value is not None else ""
