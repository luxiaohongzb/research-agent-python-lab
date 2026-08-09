from __future__ import annotations

import os
from uuid import uuid4

import pytest

from research_agent.application import ResearchApplicationService
from research_agent.distributed import (
    RedisCancellationRegistry,
    RedisExecutionLease,
    RedisRunQueue,
)
from research_agent.domain import ResearchRequest, RunStatus
from research_agent.events import RedisRunEventBroker
from research_agent.providers import CompositePaperProvider, OfflinePaperProvider
from research_agent.run_store import PostgresRunStore
from research_agent.worker import DistributedResearchWorker
from research_agent.workflow import ResearchWorkflow


def _redis_url() -> str:
    value = os.getenv("REDIS_TEST_URL")
    if not value:
        pytest.skip("REDIS_TEST_URL is not configured")
    return value


@pytest.mark.asyncio
async def test_redis_events_queue_cancellation_and_lease() -> None:
    url = _redis_url()
    prefix = f"research-test-{uuid4().hex}"
    broker = RedisRunEventBroker(url, prefix=prefix)
    queue = RedisRunQueue(
        url,
        consumer_name="test-consumer",
        group="test-workers",
        prefix=prefix,
        lease_seconds=5,
    )
    cancellations = RedisCancellationRegistry(url, prefix=prefix)
    lease = RedisExecutionLease(url, prefix=prefix, lease_seconds=5)
    try:
        first = await broker.publish("run-1", "queued", {"status": "PENDING"})
        terminal = await broker.publish("run-1", "completed", {"status": "COMPLETED"})
        replay = [item async for item in broker.stream("run-1")]
        assert [item.sequence for item in replay] == [first.sequence, terminal.sequence]
        assert (await broker.history("run-1"))[-1].event == "completed"

        await queue.enqueue("run-1", resume=True)
        job = await queue.receive(block_ms=100)
        assert job is not None
        assert job.run_id == "run-1"
        assert job.resume is True
        await queue.heartbeat(job)
        await queue.ack(job)

        await cancellations.request("run-1")
        assert await cancellations.is_requested("run-1") is True
        await cancellations.clear("run-1")
        assert await cancellations.is_requested("run-1") is False

        assert await lease.acquire("run-1", "worker-a") is True
        assert await lease.acquire("run-1", "worker-b") is False
        assert await lease.renew("run-1", "worker-a") is True
        await lease.release("run-1", "worker-a")
        assert await lease.acquire("run-1", "worker-b") is True
    finally:
        await broker.close()
        await queue.close()
        await cancellations.close()
        await lease.close()


@pytest.mark.asyncio
async def test_postgres_run_is_executed_by_independent_redis_worker() -> None:
    redis_url = _redis_url()
    postgres_dsn = os.getenv("POSTGRES_TEST_DSN")
    if not postgres_dsn:
        pytest.skip("POSTGRES_TEST_DSN is not configured")
    prefix = f"research-worker-test-{uuid4().hex}"
    group = "integration-workers"
    api_queue = RedisRunQueue(
        redis_url,
        consumer_name="api",
        group=group,
        prefix=prefix,
        lease_seconds=5,
    )
    worker_queue = RedisRunQueue(
        redis_url,
        consumer_name="worker-one",
        group=group,
        prefix=prefix,
        lease_seconds=5,
    )
    api_service = ResearchApplicationService(
        ResearchWorkflow(provider=CompositePaperProvider((OfflinePaperProvider(),))),
        PostgresRunStore(postgres_dsn),
        events=RedisRunEventBroker(redis_url, prefix=prefix),
        queue=api_queue,
        cancellations=RedisCancellationRegistry(redis_url, prefix=prefix),
    )
    worker_service = ResearchApplicationService(
        ResearchWorkflow(provider=CompositePaperProvider((OfflinePaperProvider(),))),
        PostgresRunStore(postgres_dsn),
        events=RedisRunEventBroker(redis_url, prefix=prefix),
        cancellations=RedisCancellationRegistry(redis_url, prefix=prefix),
    )
    worker = DistributedResearchWorker(
        worker_service,
        worker_queue,
        RedisExecutionLease(redis_url, prefix=prefix, lease_seconds=5),
        worker_id="worker-one",
    )
    try:
        await worker.initialize()
        submitted = await api_service.submit(
            ResearchRequest(
                question="How should research agents verify claims?",
                max_iterations=1,
            )
        )
        assert submitted.status is RunStatus.PENDING
        assert await worker.run_once(block_ms=1_000) is True

        completed = await api_service.get(submitted.run_id)
        assert completed.status in {RunStatus.COMPLETED, RunStatus.NEEDS_REVIEW}
        assert completed.result is not None
        events = await api_service.event_history(submitted.run_id)
        assert [item.event for item in events][0:2] == ["queued", "started"]
        assert events[-1].event == "completed"
    finally:
        await worker.close()
        await api_service.close()
