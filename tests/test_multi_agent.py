import asyncio
from typing import Any, cast

import pytest

from research_agent.artifacts import ArtifactNotFoundError, InMemoryArtifactStore
from research_agent.domain import (
    ArtifactKind,
    Paper,
    Passage,
    ResearchRequest,
    RunStatus,
    SearchTask,
    WorkerStatus,
)
from research_agent.providers import CompositePaperProvider, stable_paper_id
from research_agent.retrieval import RetrievalBatch
from research_agent.workflow import ResearchWorkflow


class ConcurrentProvider:
    name = "concurrent-fixture"

    def __init__(self) -> None:
        self.active = 0
        self.peak_active = 0

    async def search(self, task: SearchTask, limit: int) -> list[Paper]:
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            await asyncio.sleep(0.05)
            title = f"Study for {task.task_id}"
            return [
                Paper(
                    paper_id=stable_paper_id(doi=None, title=title),
                    title=title,
                    abstract=f"{task.query}. This study reports traceable evidence.",
                    source=self.name,
                    score=1,
                )
            ][:limit]
        finally:
            self.active -= 1


class PartiallyFailingRetriever:
    lane_count = 1

    async def search(self, task: SearchTask, limit: int) -> RetrievalBatch:
        if "limitations" in task.query.lower():
            raise RuntimeError("simulated worker failure")
        title = f"Evidence for {task.task_id}"
        paper = Paper(
            paper_id=stable_paper_id(doi=None, title=title),
            title=title,
            abstract=f"{task.query}. Independent evidence is available.",
            source="partial-fixture",
            score=1,
        )
        passage = Passage(
            paper_id=paper.paper_id,
            text=paper.abstract,
            end_char=len(paper.abstract),
        )
        return RetrievalBatch((paper,), (passage,), (), ())

    async def upsert(self, document: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_deep_question_dispatches_bounded_workers_concurrently() -> None:
    provider = ConcurrentProvider()
    workflow = ResearchWorkflow(
        provider=CompositePaperProvider((provider,)),
        artifact_store=InMemoryArtifactStore(),
    )

    result = await workflow.run(
        ResearchRequest(
            question="Compare agentic RAG and claim verification approaches",
            max_iterations=1,
            max_workers=2,
        )
    )

    assert provider.peak_active == 2
    # max_workers limits concurrent workers; remaining planned tasks continue
    # in a later bounded wave instead of being silently dropped.
    assert len(result.workers) == 3
    assert result.budget.used_workers == 3
    assert result.budget.used_queries == 3
    assert all(item.status is WorkerStatus.COMPLETED for item in result.workers)
    assert all(item.artifact_ref is not None for item in result.workers)
    assert "dispatch_workers" in [event.node for event in result.trace]
    reference = result.workers[0].artifact_ref
    assert reference is not None
    artifact = await workflow.get_artifact(reference)
    assert isinstance(artifact, RetrievalBatch)


@pytest.mark.asyncio
async def test_worker_failure_is_isolated_and_successful_artifacts_are_merged() -> None:
    workflow = ResearchWorkflow(retriever=cast(Any, PartiallyFailingRetriever()))

    result = await workflow.run(
        ResearchRequest(
            question="Compare research agent evidence strategies",
            max_iterations=1,
        )
    )

    assert result.status is RunStatus.COMPLETED
    assert {item.status for item in result.workers} == {
        WorkerStatus.COMPLETED,
        WorkerStatus.FAILED,
    }
    assert result.papers
    assert any("RuntimeError" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_worker_timeout_is_reported_without_crashing_graph() -> None:
    workflow = ResearchWorkflow(
        provider=CompositePaperProvider((ConcurrentProvider(),)),
        worker_timeout_seconds=0.001,
    )

    result = await workflow.run(
        ResearchRequest(
            question="Compare agent retrieval and verification systems",
            max_iterations=1,
        )
    )

    assert result.status is RunStatus.NEEDS_REVIEW
    assert result.workers
    assert all(item.status is WorkerStatus.TIMED_OUT for item in result.workers)
    assert all(item.error_type == "TimeoutError" for item in result.workers)


@pytest.mark.asyncio
async def test_artifacts_are_isolated_by_run_id() -> None:
    store = InMemoryArtifactStore()
    reference = await store.put(
        run_id="run-a",
        kind=ArtifactKind.RETRIEVAL_BATCH,
        value={"large": "payload"},
    )

    assert await store.get(reference.artifact_id, run_id="run-a") == {"large": "payload"}
    with pytest.raises(ArtifactNotFoundError):
        await store.get(reference.artifact_id, run_id="run-b")
