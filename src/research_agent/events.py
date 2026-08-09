from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress

from research_agent.domain import RunEvent

TERMINAL_EVENTS = frozenset({"completed", "failed", "cancelled"})


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
