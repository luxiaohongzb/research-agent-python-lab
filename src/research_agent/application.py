from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from research_agent.domain import ResearchRequest, ResearchResult, RunSnapshot, RunStatus
from research_agent.workflow import ResearchWorkflow


class RunNotFoundError(KeyError):
    pass


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, RunSnapshot] = {}
        self._lock = asyncio.Lock()

    async def save(self, snapshot: RunSnapshot) -> None:
        async with self._lock:
            self._runs[snapshot.run_id] = snapshot

    async def get(self, run_id: str) -> RunSnapshot:
        async with self._lock:
            snapshot = self._runs.get(run_id)
        if snapshot is None:
            raise RunNotFoundError(run_id)
        return snapshot


class ResearchApplicationService:
    def __init__(self, workflow: ResearchWorkflow, store: InMemoryRunStore | None = None) -> None:
        self._workflow = workflow
        self._store = store or InMemoryRunStore()
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self, request: ResearchRequest, *, run_id: str | None = None) -> ResearchResult:
        return await self._workflow.run(request, run_id=run_id)

    async def submit(self, request: ResearchRequest) -> RunSnapshot:
        run_id = uuid4().hex
        snapshot = RunSnapshot(run_id=run_id, status=RunStatus.PENDING, request=request)
        await self._store.save(snapshot)
        task = asyncio.create_task(self._execute(snapshot))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return snapshot

    async def get(self, run_id: str) -> RunSnapshot:
        return await self._store.get(run_id)

    async def _execute(self, snapshot: RunSnapshot) -> None:
        now = datetime.now(UTC)
        await self._store.save(
            snapshot.model_copy(update={"status": RunStatus.RUNNING, "updated_at": now})
        )
        try:
            result = await self._workflow.run(snapshot.request, run_id=snapshot.run_id)
            final = snapshot.model_copy(
                update={
                    "status": result.status,
                    "result": result,
                    "updated_at": datetime.now(UTC),
                }
            )
        except Exception as exc:  # failure is persisted for asynchronous callers
            final = snapshot.model_copy(
                update={
                    "status": RunStatus.FAILED,
                    "error": f"{type(exc).__name__}: {exc}",
                    "updated_at": datetime.now(UTC),
                }
            )
        await self._store.save(final)
