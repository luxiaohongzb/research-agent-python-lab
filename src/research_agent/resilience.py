from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import TypeVar

import httpx

T = TypeVar("T")
AttemptObserver = Callable[[int, float, str, bool], None]
Sleep = Callable[[float], Awaitable[None]]

_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry policy for idempotent provider operations."""

    max_attempts: int = 3
    initial_backoff_seconds: float = 0.25
    max_backoff_seconds: float = 4.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds must be non-negative")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must not be smaller than initial backoff")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    observer: AttemptObserver | None = None,
    sleep: Sleep = asyncio.sleep,
) -> T:
    """Retry a transient idempotent operation and preserve the final exception."""

    for attempt in range(1, policy.max_attempts + 1):
        started = monotonic()
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retrying = attempt < policy.max_attempts and is_transient_failure(exc)
            if observer is not None:
                observer(attempt, monotonic() - started, _failure_outcome(exc), retrying)
            if not retrying:
                raise
            await sleep(_retry_delay(exc, attempt, policy))
        else:
            if observer is not None:
                observer(attempt, monotonic() - started, "success", False)
            return result
    raise RuntimeError("retry loop exhausted without returning or raising")


def is_transient_failure(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS_CODES
    return isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ),
    )


def _retry_delay(exc: Exception, attempt: int, policy: RetryPolicy) -> float:
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        return min(policy.max_backoff_seconds, retry_after)
    exponential = min(
        policy.max_backoff_seconds,
        policy.initial_backoff_seconds * (2 ** (attempt - 1)),
    )
    if exponential == 0:
        return 0
    jitter = exponential * policy.jitter_ratio * float(random.random())
    return float(min(policy.max_backoff_seconds, exponential + jitter))


def _retry_after_seconds(exc: Exception) -> float | None:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    value = exc.response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _failure_outcome(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    return type(exc).__name__.lower()
