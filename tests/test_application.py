import asyncio

import pytest

from research_agent.application import IdempotencyConflictError, ResearchApplicationService
from research_agent.domain import Paper, ResearchRequest, RunStatus, SearchTask
from research_agent.providers import CompositePaperProvider, stable_paper_id
from research_agent.workflow import ResearchWorkflow


class PausingProvider:
    name = "pausing"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.pause = True

    async def search(self, task: SearchTask, limit: int) -> list[Paper]:
        self.started.set()
        if self.pause:
            await asyncio.sleep(60)
        return [
            Paper(
                paper_id=stable_paper_id(doi=None, title="Resumable evidence study"),
                title="Resumable evidence study",
                abstract=f"{task.query}. Evidence remains traceable after task recovery.",
                source=self.name,
                score=1,
            )
        ][:limit]


async def _wait_terminal(
    service: ResearchApplicationService, run_id: str, *, attempts: int = 100
) -> RunStatus:
    for _ in range(attempts):
        snapshot = await service.get(run_id)
        if snapshot.status in {
            RunStatus.COMPLETED,
            RunStatus.NEEDS_REVIEW,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return snapshot.status
        await asyncio.sleep(0.01)
    raise AssertionError("run did not reach a terminal state")


@pytest.mark.asyncio
async def test_idempotent_submit_returns_original_run() -> None:
    service = ResearchApplicationService(
        ResearchWorkflow(provider=CompositePaperProvider((PausingProvider(),)))
    )
    request = ResearchRequest(question="How can a research task be resumed?", max_iterations=1)
    try:
        first = await service.submit(request, idempotency_key="same-request")
        second = await service.submit(request, idempotency_key="same-request")
        assert second.run_id == first.run_id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_long_running_stage_publishes_heartbeat_events() -> None:
    provider = PausingProvider()
    service = ResearchApplicationService(
        ResearchWorkflow(provider=CompositePaperProvider((provider,))),
        progress_heartbeat_seconds=0.01,
    )
    try:
        submitted = await service.submit(
            ResearchRequest(question="How are long research stages observed?", max_iterations=1)
        )
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        await asyncio.sleep(0.04)

        events = await service.event_history(submitted.run_id)

        heartbeats = [item for item in events if item.event == "heartbeat"]
        assert heartbeats
        assert heartbeats[-1].details["node"] in {"retrieval", "research_workers"}
        assert "stage_elapsed_seconds" in heartbeats[-1].details
        step_trace = heartbeats[-1].details["step_trace"]
        assert step_trace["kind"] == "decision_summary"
        assert step_trace["reason"]
        assert step_trace["action"]
        assert step_trace["observation"]
    finally:
        await service.cancel(submitted.run_id)
        await service.close()


@pytest.mark.asyncio
async def test_progress_events_publish_auditable_reason_act_observe_summaries() -> None:
    provider = PausingProvider()
    provider.pause = False
    service = ResearchApplicationService(
        ResearchWorkflow(provider=CompositePaperProvider((provider,)))
    )
    try:
        submitted = await service.submit(
            ResearchRequest(
                question="How should research agents expose auditable execution steps?",
                max_iterations=1,
            )
        )
        await _wait_terminal(service, submitted.run_id)
        events = await service.event_history(submitted.run_id)

        progress = [item for item in events if item.event == "progress"]
        assert progress
        assert {item.details["node"] for item in progress} >= {
            "plan",
            "normalize",
            "quality_gate",
        }
        for item in progress:
            step_trace = item.details["step_trace"]
            assert step_trace["kind"] == "decision_summary"
            assert step_trace["reason"]
            assert step_trace["action"]
            assert step_trace["observation"]
            assert isinstance(step_trace["metrics"], dict)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_idempotency_key_rejects_a_different_request() -> None:
    service = ResearchApplicationService(
        ResearchWorkflow(provider=CompositePaperProvider((PausingProvider(),)))
    )
    try:
        await service.submit(
            ResearchRequest(question="How can a research task be resumed?"),
            idempotency_key="same-key-different-payload",
        )
        with pytest.raises(IdempotencyConflictError):
            await service.submit(
                ResearchRequest(question="How should evidence be reviewed?"),
                idempotency_key="same-key-different-payload",
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancelled_run_resumes_from_checkpoint() -> None:
    provider = PausingProvider()
    service = ResearchApplicationService(
        ResearchWorkflow(provider=CompositePaperProvider((provider,)))
    )
    try:
        submitted = await service.submit(
            ResearchRequest(question="How can a research task be resumed?", max_iterations=1)
        )
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        cancelled = await service.cancel(submitted.run_id)
        assert cancelled.status is RunStatus.CANCELLED

        provider.pause = False
        resumed = await service.resume(submitted.run_id)
        assert resumed.status is RunStatus.PENDING
        final_status = await _wait_terminal(service, submitted.run_id)
        assert final_status in {RunStatus.COMPLETED, RunStatus.NEEDS_REVIEW}
        events = await service.event_history(submitted.run_id)
        assert "cancelled" in [item.event for item in events]
        assert "resuming" in [item.event for item in events]
        stream = service.stream_events(
            submitted.run_id,
            after_sequence=events[-1].sequence,
        )
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await service.close()
