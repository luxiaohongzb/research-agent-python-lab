from __future__ import annotations

import argparse
import asyncio
import socket
import sys
from uuid import uuid4

from prometheus_client import start_http_server

from research_agent.application import ResearchApplicationService
from research_agent.config import Settings, get_settings
from research_agent.distributed import (
    ExecutionLease,
    QueuedRun,
    RedisCancellationRegistry,
    RedisExecutionLease,
    RedisRunQueue,
    RunQueue,
)
from research_agent.domain import RunStatus
from research_agent.events import RedisRunEventBroker
from research_agent.observability import RuntimeObservability
from research_agent.run_store import PostgresRunStore
from research_agent.workflow import build_default_workflow


class DistributedResearchWorker:
    def __init__(
        self,
        service: ResearchApplicationService,
        queue: RunQueue,
        lease: ExecutionLease,
        *,
        worker_id: str,
    ) -> None:
        self._service = service
        self._queue = queue
        self._lease = lease
        self._worker_id = worker_id

    async def run_forever(self) -> None:
        while True:
            await self.run_once()

    async def initialize(self) -> None:
        await self._service.initialize()

    async def run_once(self, *, block_ms: int = 5_000) -> bool:
        job = await self._queue.receive(block_ms=block_ms)
        if job is None:
            return False
        owner = f"{self._worker_id}:{job.message_id}"
        if not await self._lease.acquire(job.run_id, owner):
            # The message may have been reclaimed while its original worker still
            # holds a renewed run lease. Leaving it pending prevents job loss if
            # that worker fails before persisting a terminal snapshot.
            return True
        execution = asyncio.create_task(
            self._service.execute_queued(job.run_id, resume=job.resume),
            name=f"execute-{job.run_id}",
        )
        heartbeat = asyncio.create_task(
            self._renew_lease(job, owner),
            name=f"lease-{job.run_id}",
        )
        completed = False
        try:
            done, _ = await asyncio.wait(
                {execution, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if execution in done:
                await execution
                completed = True
            else:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self._lease.release(job.run_id, owner)
            if completed:
                snapshot = await self._service.get(job.run_id)
                if snapshot.status is RunStatus.FAILED:
                    await self._queue.dead_letter(job, snapshot.error or "research run failed")
                else:
                    await self._queue.ack(job)
        return True

    async def close(self) -> None:
        await self._queue.close()
        await self._lease.close()
        await self._service.close()

    async def _renew_lease(self, job: QueuedRun, owner: str) -> None:
        interval = max(1.0, self._lease.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not await self._lease.renew(job.run_id, owner):
                return
            await self._queue.heartbeat(job)


def build_worker(
    settings: Settings | None = None, *, worker_id: str | None = None
) -> DistributedResearchWorker:
    current = settings or get_settings()
    if current.run_store_mode != "postgres":
        raise RuntimeError("distributed workers require RUN_STORE_MODE=postgres")
    identity = worker_id or f"{socket.gethostname()}-{uuid4().hex[:8]}"
    queue = RedisRunQueue(
        current.redis_url,
        consumer_name=identity,
        group=current.redis_consumer_group,
        prefix=current.redis_prefix,
        lease_seconds=current.queue_lease_seconds,
    )
    service = ResearchApplicationService(
        build_default_workflow(current),
        PostgresRunStore(current.database_url),
        events=RedisRunEventBroker(current.redis_url, prefix=current.redis_prefix),
        cancellations=RedisCancellationRegistry(
            current.redis_url,
            prefix=current.redis_prefix,
        ),
        observability=RuntimeObservability(
            otel_enabled=current.otel_enabled,
            service_name=f"{current.otel_service_name}-worker",
        ),
    )
    lease = RedisExecutionLease(
        current.redis_url,
        prefix=current.redis_prefix,
        lease_seconds=current.queue_lease_seconds,
    )
    return DistributedResearchWorker(service, queue, lease, worker_id=identity)


async def _run(worker_id: str | None = None) -> None:
    settings = get_settings()
    start_http_server(settings.worker_metrics_port, addr="0.0.0.0")
    worker = build_worker(settings, worker_id=worker_id)
    await worker.initialize()
    try:
        await worker.run_forever()
    finally:
        await worker.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a distributed research worker")
    parser.add_argument("--worker-id")
    args = parser.parse_args()
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(_run(args.worker_id))
    else:
        asyncio.run(_run(args.worker_id))


if __name__ == "__main__":
    main()
