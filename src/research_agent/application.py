from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

from research_agent.domain import (
    HumanReview,
    HumanReviewRequest,
    ResearchRequest,
    ResearchResult,
    RunEvent,
    RunSnapshot,
    RunStatus,
)
from research_agent.events import InMemoryRunEventBroker
from research_agent.observability import RuntimeObservability, RuntimeSummary
from research_agent.run_store import (
    IdempotencyConflictError,
    InMemoryRunStore,
    RunNotFoundError,
    RunStore,
)
from research_agent.workflow import ResearchWorkflow

__all__ = [
    "InMemoryRunStore",
    "IdempotencyConflictError",
    "ResearchApplicationService",
    "ReviewValidationError",
    "RunNotFoundError",
]

TERMINAL_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.NEEDS_REVIEW, RunStatus.FAILED, RunStatus.CANCELLED}
)


class ReviewValidationError(ValueError):
    pass


class ResearchApplicationService:
    def __init__(
        self,
        workflow: ResearchWorkflow,
        store: RunStore | None = None,
        *,
        events: InMemoryRunEventBroker | None = None,
        observability: RuntimeObservability | None = None,
    ) -> None:
        self._workflow = workflow
        self._store = store or InMemoryRunStore()
        self._events = events or InMemoryRunEventBroker()
        self._observability = observability or RuntimeObservability()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def run(self, request: ResearchRequest, *, run_id: str | None = None) -> ResearchResult:
        await self._observability.started()
        try:
            with self._observability.span(
                "research.run",
                {"research.run_id": run_id or "synchronous"},
            ):
                result = await self._workflow.run(request, run_id=run_id)
        except BaseException:
            await self._observability.failed(RunStatus.FAILED.value)
            raise
        await self._observability.finished(result)
        return result

    async def submit(
        self,
        request: ResearchRequest,
        *,
        idempotency_key: str | None = None,
    ) -> RunSnapshot:
        run_id = uuid4().hex
        candidate = RunSnapshot(
            run_id=run_id,
            status=RunStatus.PENDING,
            request=request,
            idempotency_key=idempotency_key,
        )
        snapshot = await self._store.create(candidate)
        if snapshot.run_id != run_id:
            return snapshot
        await self._events.publish(run_id, "queued", {"status": snapshot.status.value})
        task = asyncio.create_task(self._execute(snapshot), name=f"research-run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
        return snapshot

    async def get(self, run_id: str) -> RunSnapshot:
        return await self._store.get(run_id)

    async def cancel(self, run_id: str) -> RunSnapshot:
        snapshot = await self._store.get(run_id)
        if snapshot.status in TERMINAL_STATUSES:
            return snapshot
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            latest = await self._store.get(run_id)
            if latest.status is RunStatus.CANCELLED:
                return latest
            snapshot = latest
        cancelled = snapshot.model_copy(
            update={"status": RunStatus.CANCELLED, "updated_at": datetime.now(UTC)}
        )
        await self._store.save(cancelled)
        await self._events.publish(run_id, "cancelled", {"status": RunStatus.CANCELLED.value})
        return cancelled

    async def resume(self, run_id: str) -> RunSnapshot:
        snapshot = await self._store.get(run_id)
        if snapshot.status in {RunStatus.COMPLETED, RunStatus.NEEDS_REVIEW}:
            return snapshot
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return snapshot
        pending = snapshot.model_copy(
            update={
                "status": RunStatus.PENDING,
                "error": None,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._store.save(pending)
        await self._events.publish(run_id, "resuming", {"status": RunStatus.PENDING.value})
        task = asyncio.create_task(
            self._execute(pending, resume=True),
            name=f"research-resume-{run_id}",
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
        return pending

    async def review(self, run_id: str, request: HumanReviewRequest) -> RunSnapshot:
        snapshot = await self._store.get(run_id)
        if snapshot.result is None:
            raise ReviewValidationError("run has no result to review")
        claim_ids = {item.claim_id for item in snapshot.result.claims}
        evidence_ids = {item.evidence_id for item in snapshot.result.evidence}
        if request.claim_id and request.claim_id not in claim_ids:
            raise ReviewValidationError("unknown claim_id")
        if request.evidence_id and request.evidence_id not in evidence_ids:
            raise ReviewValidationError("unknown evidence_id")
        review = HumanReview(**request.model_dump())
        updated = snapshot.model_copy(
            update={
                "reviews": (*snapshot.reviews, review),
                "updated_at": datetime.now(UTC),
            }
        )
        await self._store.save(updated)
        await self._events.publish(
            run_id,
            "reviewed",
            {
                "review_id": review.review_id,
                "decision": review.decision.value,
                "claim_id": review.claim_id,
                "evidence_id": review.evidence_id,
            },
        )
        return updated

    async def stream_events(
        self, run_id: str, *, after_sequence: int = 0
    ) -> AsyncIterator[RunEvent]:
        await self._store.get(run_id)
        async for event in self._events.stream(run_id, after_sequence=after_sequence):
            yield event

    async def metrics_summary(self) -> RuntimeSummary:
        return await self._observability.summary()

    async def event_history(self, run_id: str) -> tuple[RunEvent, ...]:
        await self._store.get(run_id)
        return await self._events.history(run_id)

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._store.close()
        await self._workflow.close()

    async def _execute(self, snapshot: RunSnapshot, *, resume: bool = False) -> None:
        running = snapshot.model_copy(
            update={"status": RunStatus.RUNNING, "updated_at": datetime.now(UTC)}
        )
        await self._store.save(running)
        await self._events.publish(snapshot.run_id, "started", {"status": RunStatus.RUNNING.value})
        await self._observability.started()

        async def progress(node: str, details: dict[str, object]) -> None:
            await self._events.publish(
                snapshot.run_id,
                "progress",
                {"node": node, **details},
            )

        try:
            with self._observability.span(
                "research.run",
                {"research.run_id": snapshot.run_id, "research.async": True},
            ):
                if resume:
                    result = await self._workflow.resume(
                        snapshot.request,
                        run_id=snapshot.run_id,
                        progress=progress,
                    )
                else:
                    result = await self._workflow.run(
                        snapshot.request,
                        run_id=snapshot.run_id,
                        progress=progress,
                    )
            final = running.model_copy(
                update={
                    "status": result.status,
                    "result": result,
                    "updated_at": datetime.now(UTC),
                }
            )
            await self._store.save(final)
            await self._observability.finished(result)
            await self._events.publish(
                snapshot.run_id,
                "completed",
                {"status": result.status.value},
            )
        except asyncio.CancelledError:
            cancelled = running.model_copy(
                update={
                    "status": RunStatus.CANCELLED,
                    "updated_at": datetime.now(UTC),
                }
            )
            await self._store.save(cancelled)
            await self._observability.failed(RunStatus.CANCELLED.value)
            await self._events.publish(
                snapshot.run_id,
                "cancelled",
                {"status": RunStatus.CANCELLED.value},
            )
            raise
        except Exception as exc:  # failure is persisted for asynchronous callers
            failed = running.model_copy(
                update={
                    "status": RunStatus.FAILED,
                    "error": f"{type(exc).__name__}: {exc}",
                    "updated_at": datetime.now(UTC),
                }
            )
            await self._store.save(failed)
            await self._observability.failed(RunStatus.FAILED.value)
            await self._events.publish(
                snapshot.run_id,
                "failed",
                {"status": RunStatus.FAILED.value, "error_type": type(exc).__name__},
            )
