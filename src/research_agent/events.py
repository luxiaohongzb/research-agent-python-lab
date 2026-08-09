from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any, Protocol

from research_agent.domain import RunEvent

TERMINAL_EVENTS = frozenset({"completed", "failed", "cancelled"})


class RunEventBroker(Protocol):
    async def publish(
        self,
        run_id: str,
        event: str,
        details: dict[str, object] | None = None,
    ) -> RunEvent: ...

    def stream(self, run_id: str, *, after_sequence: int = 0) -> AsyncIterator[RunEvent]: ...

    async def history(self, run_id: str) -> tuple[RunEvent, ...]: ...

    async def close(self) -> None: ...


class InMemoryRunEventBroker:
    """Bounded replay plus live fan-out for run progress events."""

    def __init__(self, *, max_history_per_run: int = 200, queue_size: int = 100) -> None:
        self._max_history = max_history_per_run
        self._queue_size = queue_size
        self._history: dict[str, list[RunEvent]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[RunEvent]]] = {}
        self._lock = asyncio.Lock()

    async def publish(
        self,
        run_id: str,
        event: str,
        details: dict[str, object] | None = None,
    ) -> RunEvent:
        async with self._lock:
            history = self._history.setdefault(run_id, [])
            item = RunEvent(
                sequence=(history[-1].sequence + 1) if history else 1,
                run_id=run_id,
                event=event,
                details=dict(details or {}),
            )
            history.append(item)
            if len(history) > self._max_history:
                del history[: len(history) - self._max_history]
            subscribers = tuple(self._subscribers.get(run_id, ()))
        for queue in subscribers:
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(item)
        return item

    async def stream(self, run_id: str, *, after_sequence: int = 0) -> AsyncIterator[RunEvent]:
        queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            replay = [
                item for item in self._history.get(run_id, ()) if item.sequence > after_sequence
            ]
            history_is_terminal = bool(
                self._history.get(run_id) and self._history[run_id][-1].event in TERMINAL_EVENTS
            )
            self._subscribers.setdefault(run_id, set()).add(queue)
        try:
            for item in replay:
                yield item
                if item.event in TERMINAL_EVENTS:
                    return
            if history_is_terminal:
                return
            while True:
                item = await queue.get()
                yield item
                if item.event in TERMINAL_EVENTS:
                    return
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(run_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(run_id, None)

    async def history(self, run_id: str) -> tuple[RunEvent, ...]:
        async with self._lock:
            return tuple(self._history.get(run_id, ()))

    async def close(self) -> None:
        return None


class RedisRunEventBroker:
    """Redis Streams event log with bounded replay and cross-process SSE fan-out."""

    def __init__(
        self,
        redis_url: str,
        *,
        prefix: str = "research-agent",
        max_history_per_run: int = 500,
        block_ms: int = 15_000,
        client: Any = None,
    ) -> None:
        self._redis_url = redis_url
        self._prefix = prefix
        self._max_history = max_history_per_run
        self._block_ms = block_ms
        self._client = client

    async def publish(
        self,
        run_id: str,
        event: str,
        details: dict[str, object] | None = None,
    ) -> RunEvent:
        client = self._get_client()
        sequence = int(await client.incr(self._sequence_key(run_id)))
        item = RunEvent(
            sequence=sequence,
            run_id=run_id,
            event=event,
            details=dict(details or {}),
        )
        await client.xadd(
            self._stream_key(run_id),
            {
                "sequence": str(item.sequence),
                "event": item.event,
                "details": json.dumps(item.details, ensure_ascii=False, separators=(",", ":")),
                "occurred_at": item.occurred_at.isoformat(),
            },
            maxlen=self._max_history,
            approximate=True,
        )
        return item

    async def stream(self, run_id: str, *, after_sequence: int = 0) -> AsyncIterator[RunEvent]:
        client = self._get_client()
        stream_key = self._stream_key(run_id)
        history = await client.xrange(stream_key)
        last_stream_id = history[-1][0] if history else "0-0"
        for stream_id, fields in history:
            item = _redis_event(run_id, fields)
            if item.sequence <= after_sequence:
                continue
            yield item
            if item.event in TERMINAL_EVENTS:
                return
            last_stream_id = stream_id
        if history and _redis_event(run_id, history[-1][1]).event in TERMINAL_EVENTS:
            return
        while True:
            response = await client.xread(
                {stream_key: last_stream_id},
                count=100,
                block=self._block_ms,
            )
            if not response:
                continue
            for _, messages in response:
                for stream_id, fields in messages:
                    last_stream_id = stream_id
                    item = _redis_event(run_id, fields)
                    if item.sequence <= after_sequence:
                        continue
                    yield item
                    if item.event in TERMINAL_EVENTS:
                        return

    async def history(self, run_id: str) -> tuple[RunEvent, ...]:
        history = await self._get_client().xrange(self._stream_key(run_id))
        return tuple(_redis_event(run_id, fields) for _, fields in history)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                redis = importlib.import_module("redis.asyncio")
            except ImportError as exc:
                raise RuntimeError(
                    'Redis events require: python -m pip install -e ".[distributed]"'
                ) from exc
            self._client = redis.Redis.from_url(self._redis_url, decode_responses=True)
        return self._client

    def _stream_key(self, run_id: str) -> str:
        return f"{self._prefix}:run:{run_id}:events"

    def _sequence_key(self, run_id: str) -> str:
        return f"{self._prefix}:run:{run_id}:event-sequence"


def _redis_event(run_id: str, fields: dict[str, str]) -> RunEvent:
    return RunEvent.model_validate(
        {
            "sequence": int(fields["sequence"]),
            "run_id": run_id,
            "event": fields["event"],
            "details": json.loads(fields.get("details", "{}")),
            "occurred_at": fields["occurred_at"],
        }
    )
