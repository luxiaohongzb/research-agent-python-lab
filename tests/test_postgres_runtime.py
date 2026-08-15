import os
from uuid import uuid4

import asyncpg
import pytest

from research_agent.auth import Role, TokenManager
from research_agent.domain import ResearchRequest, RunSnapshot, RunStatus
from research_agent.identity import (
    CreateUserRequest,
    IdentityService,
    LoginRequest,
    PostgresUserStore,
)
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


@pytest.mark.asyncio
async def test_postgres_identity_persists_users_and_refresh_sessions() -> None:
    dsn = _dsn()
    tenant_id = f"identity-{uuid4().hex}"
    email = f"admin-{uuid4().hex}@example.com"
    tokens = TokenManager("postgres-identity-test-secret-with-at-least-32-bytes")
    first_store = PostgresUserStore(dsn)
    first_identity = IdentityService(first_store, tokens)
    try:
        user = await first_identity.create_user(
            tenant_id=tenant_id,
            request=CreateUserRequest(
                email=email,
                display_name="Persistent Administrator",
                password="Persistent password 123!",
                roles=frozenset({Role.ADMIN}),
            ),
            actor="system:test",
        )
        pair = await first_identity.login(
            LoginRequest(
                tenant_id=tenant_id,
                email=email,
                password="Persistent password 123!",
            )
        )
    finally:
        await first_identity.close()

    second_store = PostgresUserStore(dsn)
    second_identity = IdentityService(second_store, tokens)
    try:
        restored = await second_identity.get_user(user.user_id, tenant_id=tenant_id)
        rotated = await second_identity.refresh(pair.refresh_token)
    finally:
        await second_identity.close()

    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute("DELETE FROM identity_users WHERE tenant_id = $1", tenant_id)
    finally:
        await connection.close()

    assert restored.email == email
    assert rotated.refresh_token != pair.refresh_token
