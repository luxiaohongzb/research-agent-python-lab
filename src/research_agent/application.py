from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

from research_agent.audit import AuditEvent, AuditStore, InMemoryAuditStore
from research_agent.distributed import (
    CancellationRegistry,
    InMemoryCancellationRegistry,
    RunCancellationRequested,
    RunQueue,
)
from research_agent.domain import (
    HumanReview,
    HumanReviewRequest,
    ResearchRequest,
    ResearchResult,
    RunEvent,
    RunSnapshot,
    RunStatus,
)
from research_agent.events import InMemoryRunEventBroker, RunEventBroker
from research_agent.governance import QuotaPolicy
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

ACTIVE_STAGE_AFTER_NODE = {
    "plan": "retrieval",
    "search": "normalize",
    "dispatch_workers": "research_workers",
    "research_worker": "research_workers",
    "collect_workers": "normalize",
    "normalize": "extract_evidence",
    "extract_evidence": "assess_coverage",
    "assess_coverage": "synthesize",
    "refine": "retrieval",
    "synthesize": "split_claims",
    "split_claims": "verify",
    "verify": "quality_gate",
    "quality_gate": "finalize",
}


class ReviewValidationError(ValueError):
    pass


class ResearchApplicationService:
    def __init__(
        self,
        workflow: ResearchWorkflow,
        store: RunStore | None = None,
        *,
        events: RunEventBroker | None = None,
        queue: RunQueue | None = None,
        cancellations: CancellationRegistry | None = None,
        observability: RuntimeObservability | None = None,
        audit: AuditStore | None = None,
        quota: QuotaPolicy | None = None,
        progress_heartbeat_seconds: float = 10.0,
    ) -> None:
        self._workflow = workflow
        self._store = store or InMemoryRunStore()
        self._events = events or InMemoryRunEventBroker()
        self._queue = queue
        self._cancellations = cancellations or InMemoryCancellationRegistry()
        self._observability = observability or RuntimeObservability()
        self._audit = audit or InMemoryAuditStore()
        self._quota = quota
        self._progress_heartbeat_seconds = progress_heartbeat_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def run(
        self,
        request: ResearchRequest,
        *,
        run_id: str | None = None,
        tenant_id: str = "default",
        actor: str = "system",
    ) -> ResearchResult:
        if self._quota is not None:
            await self._quota.enforce(tenant_id, request)
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
        await self._audit.append(
            AuditEvent(
                tenant_id=tenant_id,
                actor=actor,
                action="research.synchronous_completed",
                run_id=result.run_id,
                details={"status": result.status.value},
            )
        )
        return result

    async def initialize(self) -> None:
        await self._workflow.initialize()

    async def submit(
        self,
        request: ResearchRequest,
        *,
        idempotency_key: str | None = None,
        tenant_id: str = "default",
        actor: str = "system",
    ) -> RunSnapshot:
        run_id = uuid4().hex
        candidate = RunSnapshot(
            run_id=run_id,
            tenant_id=tenant_id,
            status=RunStatus.PENDING,
            request=request,
            idempotency_key=idempotency_key,
        )
        if idempotency_key:
            existing = await self._store.find_by_idempotency(tenant_id, idempotency_key)
            if existing is not None:
                snapshot = await self._store.create(candidate)
                if self._queue is not None and snapshot.status is RunStatus.PENDING:
                    await self._queue.enqueue(snapshot.run_id)
                return snapshot
        if self._quota is not None:
            await self._quota.enforce(tenant_id, request)
        snapshot = await self._store.create(candidate)
        if snapshot.run_id != run_id:
            if self._queue is not None and snapshot.status is RunStatus.PENDING:
                await self._queue.enqueue(snapshot.run_id)
            return snapshot
        await self._events.publish(run_id, "queued", {"status": snapshot.status.value})
        await self._record("research.submitted", snapshot, actor)
        await self._dispatch(snapshot)
        return snapshot

    async def get(self, run_id: str, *, tenant_id: str | None = None) -> RunSnapshot:
        return await self._store.get(run_id, tenant_id=tenant_id)

    async def cancel(
        self, run_id: str, *, tenant_id: str | None = None, actor: str = "system"
    ) -> RunSnapshot:
        snapshot = await self._store.get(run_id, tenant_id=tenant_id)
        if snapshot.status in TERMINAL_STATUSES:
            return snapshot
        await self._cancellations.request(run_id)
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            latest = await self._store.get(run_id)
            if latest.status is RunStatus.CANCELLED:
                await self._record("research.cancelled", latest, actor)
                return latest
            snapshot = latest
        cancelled = await self._mark_cancelled(snapshot)
        await self._record("research.cancelled", cancelled, actor)
        return cancelled

    async def resume(
        self, run_id: str, *, tenant_id: str | None = None, actor: str = "system"
    ) -> RunSnapshot:
        snapshot = await self._store.get(run_id, tenant_id=tenant_id)
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
        await self._cancellations.clear(run_id)
        await self._store.save(pending)
        await self._events.publish(run_id, "resuming", {"status": RunStatus.PENDING.value})
        await self._record("research.resumed", pending, actor)
        await self._dispatch(pending, resume=True)
        return pending

    async def review(
        self,
        run_id: str,
        request: HumanReviewRequest,
        *,
        tenant_id: str | None = None,
        actor: str = "system",
    ) -> RunSnapshot:
        snapshot = await self._store.get(run_id, tenant_id=tenant_id)
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
        await self._record(
            "research.reviewed",
            updated,
            actor,
            {"review_id": review.review_id, "decision": review.decision.value},
        )
        return updated

    async def stream_events(
        self, run_id: str, *, after_sequence: int = 0, tenant_id: str | None = None
    ) -> AsyncIterator[RunEvent]:
        snapshot = await self._store.get(run_id, tenant_id=tenant_id)
        history = await self._events.history(run_id)
        if not history and snapshot.status in TERMINAL_STATUSES:
            yield RunEvent(
                sequence=1,
                run_id=run_id,
                event=snapshot.status.value.lower(),
                details={"status": snapshot.status.value, "recovered_from_snapshot": True},
            )
            return
        async for event in self._events.stream(run_id, after_sequence=after_sequence):
            yield event

    async def metrics_summary(self) -> RuntimeSummary:
        return await self._observability.summary()

    async def event_history(
        self, run_id: str, *, tenant_id: str | None = None
    ) -> tuple[RunEvent, ...]:
        await self._store.get(run_id, tenant_id=tenant_id)
        return await self._events.history(run_id)

    async def audit_events(self, tenant_id: str, *, limit: int = 100) -> tuple[AuditEvent, ...]:
        return await self._audit.list(tenant_id, limit=limit)

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._events.close()
        if self._queue is not None:
            await self._queue.close()
        await self._cancellations.close()
        await self._audit.close()
        await self._store.close()
        await self._workflow.close()

    async def execute_queued(self, run_id: str, *, resume: bool = False) -> None:
        snapshot = await self._store.get(run_id)
        if snapshot.status in {RunStatus.COMPLETED, RunStatus.NEEDS_REVIEW}:
            return
        if snapshot.status is RunStatus.CANCELLED and not resume:
            return
        should_resume = resume or snapshot.status in {RunStatus.RUNNING, RunStatus.FAILED}
        await self._execute(
            snapshot,
            resume=should_resume,
            persist_task_cancellation=False,
        )

    async def _dispatch(self, snapshot: RunSnapshot, *, resume: bool = False) -> None:
        if self._queue is not None:
            await self._queue.enqueue(snapshot.run_id, resume=resume)
            return
        task = asyncio.create_task(
            self._execute(snapshot, resume=resume),
            name=f"research-run-{snapshot.run_id}",
        )
        self._tasks[snapshot.run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(snapshot.run_id, None))

    async def _execute(
        self,
        snapshot: RunSnapshot,
        *,
        resume: bool = False,
        persist_task_cancellation: bool = True,
    ) -> None:
        if await self._cancellations.is_requested(snapshot.run_id):
            await self._mark_cancelled(snapshot)
            return
        running = snapshot.model_copy(
            update={"status": RunStatus.RUNNING, "updated_at": datetime.now(UTC)}
        )
        await self._store.save(running)
        await self._events.publish(snapshot.run_id, "started", {"status": RunStatus.RUNNING.value})
        await self._observability.started()
        run_started = time.monotonic()
        stage_started = run_started
        active_stage = "plan"

        async def progress(node: str, details: dict[str, object]) -> None:
            nonlocal active_stage, stage_started
            if await self._cancellations.is_requested(snapshot.run_id):
                raise RunCancellationRequested(snapshot.run_id)
            next_stage = ACTIVE_STAGE_AFTER_NODE.get(node, node)
            await self._events.publish(
                snapshot.run_id,
                "progress",
                {
                    "node": node,
                    "next_node": next_stage,
                    "stage_status": "completed",
                    **details,
                },
            )
            active_stage = next_stage
            stage_started = time.monotonic()

        async def publish_heartbeats() -> None:
            while True:
                await asyncio.sleep(self._progress_heartbeat_seconds)
                now = time.monotonic()
                await self._events.publish(
                    snapshot.run_id,
                    "heartbeat",
                    {
                        "node": active_stage,
                        "elapsed_seconds": int(now - run_started),
                        "stage_elapsed_seconds": int(now - stage_started),
                    },
                )

        heartbeat = asyncio.create_task(
            publish_heartbeats(),
            name=f"research-heartbeat-{snapshot.run_id}",
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
            if await self._cancellations.is_requested(snapshot.run_id):
                raise RunCancellationRequested(snapshot.run_id)
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
        except RunCancellationRequested:
            await self._mark_cancelled(running)
            await self._observability.failed(RunStatus.CANCELLED.value)
        except asyncio.CancelledError:
            if persist_task_cancellation:
                await self._mark_cancelled(running)
                await self._observability.failed(RunStatus.CANCELLED.value)
            else:
                await self._observability.interrupted()
            raise
        except Exception as exc:  # failure is persisted for asynchronous callers
            if await self._cancellations.is_requested(snapshot.run_id):
                await self._mark_cancelled(running)
                await self._observability.failed(RunStatus.CANCELLED.value)
                return
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
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _mark_cancelled(self, snapshot: RunSnapshot) -> RunSnapshot:
        latest = await self._store.get(snapshot.run_id)
        if latest.status is RunStatus.CANCELLED:
            return latest
        cancelled = latest.model_copy(
            update={
                "status": RunStatus.CANCELLED,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._store.save(cancelled)
        await self._events.publish(
            snapshot.run_id,
            "cancelled",
            {"status": RunStatus.CANCELLED.value},
        )
        return cancelled

    async def _record(
        self,
        action: str,
        snapshot: RunSnapshot,
        actor: str,
        details: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        await self._audit.append(
            AuditEvent(
                tenant_id=snapshot.tenant_id,
                actor=actor,
                action=action,
                run_id=snapshot.run_id,
                details=details or {},
            )
        )
