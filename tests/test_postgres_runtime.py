import os
from uuid import uuid4

import asyncpg
import pytest

from research_agent.domain import ResearchRequest, RunSnapshot, RunStatus
from research_agent.providers import CompositePaperProvider, OfflinePaperProvider
from research_agent.run_store import PostgresRunStore
from research_agent.workflow import ResearchWorkflow


def _dsn() -> str:
    value = os.getenv("POSTGRES_TEST_DSN")
    if not value:
        pytest.skip("POSTGRES_TEST_DSN is not configured")
    return value


@pytest.mark.asyncio
async def test_postgres_run_store_persists_idempotent_snapshot() -> None:
    dsn = _dsn()
    key = f"test-{uuid4().hex}"
    first_store = PostgresRunStore(dsn)
    first = RunSnapshot(
        run_id=uuid4().hex,
        status=RunStatus.PENDING,
        request=ResearchRequest(question="How is runtime state persisted?"),
        idempotency_key=key,
    )
    duplicate = first.model_copy(update={"run_id": uuid4().hex})
    try:
        created = await first_store.create(first)
        existing = await first_store.create(duplicate)
        await first_store.save(created.model_copy(update={"status": RunStatus.RUNNING}))
    finally:
        await first_store.close()

    second_store = PostgresRunStore(dsn)
    try:
        restored = await second_store.get(first.run_id)
    finally:
        await second_store.close()

    assert existing.run_id == created.run_id
    assert restored.status is RunStatus.RUNNING
    assert restored.idempotency_key == key


@pytest.mark.asyncio
async def test_langgraph_checkpoints_are_persisted_to_postgres() -> None:
    dsn = _dsn()
    run_id = f"checkpoint-{uuid4().hex}"
    workflow = ResearchWorkflow(
        provider=CompositePaperProvider((OfflinePaperProvider(),)),
        checkpoint_dsn=dsn,
    )
    try:
        result = await workflow.run(
            ResearchRequest(
                question="How should research agents verify claims?",
                max_iterations=1,
            ),
            run_id=run_id,
        )
    finally:
        await workflow.close()

    connection = await asyncpg.connect(dsn)
    try:
        checkpoint_count = await connection.fetchval(
            "SELECT count(*) FROM checkpoints WHERE thread_id = $1", run_id
        )
    finally:
        await connection.close()

    assert result.trace
    assert checkpoint_count > 0
