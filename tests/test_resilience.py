from __future__ import annotations

import httpx
import pytest

from research_agent.resilience import RetryPolicy, retry_async


def _status_error(status_code: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://provider.example/search")
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    response = httpx.Response(status_code, request=request, headers=headers)
    return httpx.HTTPStatusError("provider error", request=request, response=response)


@pytest.mark.asyncio
async def test_retry_async_retries_transient_status_and_records_attempts() -> None:
    calls = 0
    sleeps: list[float] = []
    attempts: list[tuple[int, str, bool]] = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _status_error(429, retry_after="0")
        return "ok"

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await retry_async(
        operation,
        policy=RetryPolicy(max_attempts=3, initial_backoff_seconds=0),
        observer=lambda attempt, _, outcome, retrying: attempts.append(
            (attempt, outcome, retrying)
        ),
        sleep=sleep,
    )

    assert result == "ok"
    assert calls == 2
    assert sleeps == [0]
    assert attempts == [(1, "http_429", True), (2, "success", False)]


@pytest.mark.asyncio
async def test_retry_async_does_not_retry_permanent_client_error() -> None:
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise _status_error(400)

    with pytest.raises(httpx.HTTPStatusError):
        await retry_async(
            operation,
            policy=RetryPolicy(max_attempts=3, initial_backoff_seconds=0),
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_retry_async_stops_at_attempt_budget() -> None:
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow provider")

    with pytest.raises(httpx.ReadTimeout):
        await retry_async(
            operation,
            policy=RetryPolicy(max_attempts=2, initial_backoff_seconds=0),
        )

    assert calls == 2
