from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from typing import Any, Protocol


class RunCancellationRequested(RuntimeError):
    pass


class CancellationRegistry(Protocol):
    async def request(self, run_id: str) -> None: ...

    async def clear(self, run_id: str) -> None: ...

    async def is_requested(self, run_id: str) -> bool: ...

    async def close(self) -> None: ...


class InMemoryCancellationRegistry:
    def __init__(self) -> None:
        self._cancelled: set[str] = set()
        self._lock = asyncio.Lock()

    async def request(self, run_id: str) -> None:
        async with self._lock:
            self._cancelled.add(run_id)

    async def clear(self, run_id: str) -> None:
        async with self._lock:
            self._cancelled.discard(run_id)

    async def is_requested(self, run_id: str) -> bool:
        async with self._lock:
            return run_id in self._cancelled

    async def close(self) -> None:
        return None


class RedisCancellationRegistry:
    def __init__(
        self,
        redis_url: str,
        *,
        prefix: str = "research-agent",
        ttl_seconds: int = 86_400,
        client: Any = None,
    ) -> None:
        self._redis_url = redis_url
        self._prefix = prefix
        self._ttl_seconds = ttl_seconds
        self._client = client

    async def request(self, run_id: str) -> None:
        await self._get_client().set(self._key(run_id), "1", ex=self._ttl_seconds)

    async def clear(self, run_id: str) -> None:
        await self._get_client().delete(self._key(run_id))

    async def is_requested(self, run_id: str) -> bool:
        return bool(await self._get_client().exists(self._key(run_id)))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = _redis_client(self._redis_url)
        return self._client

    def _key(self, run_id: str) -> str:
        return f"{self._prefix}:run:{run_id}:cancelled"


@dataclass(frozen=True)
class QueuedRun:
    message_id: str
    run_id: str
    resume: bool


@dataclass(frozen=True)
class DeadLetter:
    message_id: str
    run_id: str
    error: str


class RunQueue(Protocol):
    async def enqueue(self, run_id: str, *, resume: bool = False) -> str: ...

    async def receive(self, *, block_ms: int = 5_000) -> QueuedRun | None: ...

    async def ack(self, job: QueuedRun) -> None: ...

    async def heartbeat(self, job: QueuedRun) -> None: ...

    async def dead_letter(self, job: QueuedRun, error: str) -> None: ...

    async def dead_letters(self, *, limit: int = 100) -> tuple[DeadLetter, ...]: ...

    async def close(self) -> None: ...


class ExecutionLease(Protocol):
    @property
    def lease_seconds(self) -> int: ...

    async def acquire(self, run_id: str, owner: str) -> bool: ...

    async def renew(self, run_id: str, owner: str) -> bool: ...

    async def release(self, run_id: str, owner: str) -> None: ...

    async def close(self) -> None: ...


class RedisExecutionLease:
    def __init__(
        self,
        redis_url: str,
        *,
        prefix: str = "research-agent",
        lease_seconds: int = 60,
        client: Any = None,
    ) -> None:
        self._redis_url = redis_url
        self._prefix = prefix
        self._lease_seconds = lease_seconds
        self._client = client

    @property
    def lease_seconds(self) -> int:
        return self._lease_seconds

    async def acquire(self, run_id: str, owner: str) -> bool:
        return bool(
            await self._get_client().set(
                self._key(run_id),
                owner,
                nx=True,
                ex=self._lease_seconds,
            )
        )

    async def renew(self, run_id: str, owner: str) -> bool:
        result = await self._get_client().eval(
            _RENEW_LEASE_SCRIPT,
            1,
            self._key(run_id),
            owner,
            str(self._lease_seconds),
        )
        return bool(result)

    async def release(self, run_id: str, owner: str) -> None:
        await self._get_client().eval(
            _RELEASE_LEASE_SCRIPT,
            1,
            self._key(run_id),
            owner,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = _redis_client(self._redis_url)
        return self._client

    def _key(self, run_id: str) -> str:
        return f"{self._prefix}:run:{run_id}:lease"


class RedisRunQueue:
    """Redis Streams consumer-group queue with idle-message reclamation."""

    def __init__(
        self,
        redis_url: str,
        *,
        consumer_name: str,
        group: str = "research-workers",
        prefix: str = "research-agent",
        lease_seconds: int = 60,
        client: Any = None,
    ) -> None:
        self._redis_url = redis_url
        self._consumer_name = consumer_name
        self._group = group
        self._stream = f"{prefix}:queue:runs"
        self._dead_letter_stream = f"{prefix}:queue:dead-letters"
        self._lease_ms = lease_seconds * 1_000
        self._client = client
        self._ready = False
        self._lock = asyncio.Lock()

    async def enqueue(self, run_id: str, *, resume: bool = False) -> str:
        await self._ensure_group()
        return str(
            await self._get_client().xadd(
                self._stream,
                {"run_id": run_id, "resume": "1" if resume else "0"},
                maxlen=10_000,
                approximate=True,
            )
        )

    async def receive(self, *, block_ms: int = 5_000) -> QueuedRun | None:
        await self._ensure_group()
        claimed = await self._get_client().xautoclaim(
            self._stream,
            self._group,
            self._consumer_name,
            min_idle_time=self._lease_ms,
            start_id="0-0",
            count=1,
        )
        claimed_messages = claimed[1] if len(claimed) > 1 else ()
        if claimed_messages:
            return _queued_run(claimed_messages[0])
        response = await self._get_client().xreadgroup(
            self._group,
            self._consumer_name,
            {self._stream: ">"},
            count=1,
            block=block_ms,
        )
        if not response:
            return None
        return _queued_run(response[0][1][0])

    async def ack(self, job: QueuedRun) -> None:
        client = self._get_client()
        await client.xack(self._stream, self._group, job.message_id)
        await client.xdel(self._stream, job.message_id)

    async def heartbeat(self, job: QueuedRun) -> None:
        await self._get_client().xclaim(
            self._stream,
            self._group,
            self._consumer_name,
            min_idle_time=0,
            message_ids=[job.message_id],
            justid=True,
        )

    async def dead_letter(self, job: QueuedRun, error: str) -> None:
        await self._get_client().xadd(
            self._dead_letter_stream,
            {"run_id": job.run_id, "error": error[:2_000]},
            maxlen=10_000,
            approximate=True,
        )
        await self.ack(job)

    async def dead_letters(self, *, limit: int = 100) -> tuple[DeadLetter, ...]:
        messages = await self._get_client().xrevrange(
            self._dead_letter_stream,
            count=limit,
        )
        return tuple(
            DeadLetter(
                message_id=str(message_id),
                run_id=fields["run_id"],
                error=fields.get("error", "unknown failure"),
            )
            for message_id, fields in messages
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_group(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            try:
                await self._get_client().xgroup_create(
                    self._stream,
                    self._group,
                    id="0-0",
                    mkstream=True,
                )
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            self._ready = True

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = _redis_client(self._redis_url)
        return self._client


def _redis_client(redis_url: str) -> Any:
    try:
        redis = importlib.import_module("redis.asyncio")
    except ImportError as exc:
        raise RuntimeError(
            'Redis distribution requires: python -m pip install -e ".[distributed]"'
        ) from exc
    return redis.Redis.from_url(redis_url, decode_responses=True)


def _queued_run(message: tuple[str, dict[str, str]]) -> QueuedRun:
    message_id, fields = message
    return QueuedRun(
        message_id=str(message_id),
        run_id=fields["run_id"],
        resume=fields.get("resume") == "1",
    )


_RENEW_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
