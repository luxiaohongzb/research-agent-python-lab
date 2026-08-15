import json
import re

import httpx
import pytest

from research_agent.api import create_app
from research_agent.application import ResearchApplicationService
from research_agent.auth import Authenticator
from research_agent.domain import ResearchRequest
from research_agent.governance import QuotaExceededError, QuotaPolicy, TenantQuota
from research_agent.run_store import InMemoryRunStore
from research_agent.workflow import build_default_workflow


class _HoldingQueue:
    async def enqueue(self, run_id: str, *, resume: bool = False) -> str:
        return run_id

    async def close(self) -> None:
        return None


def _authenticator() -> Authenticator:
    identities = {
        "tenant-a-admin": {
            "subject": "alice@example.com",
            "tenant_id": "tenant-a",
            "roles": ["ADMIN", "RESEARCHER", "REVIEWER"],
        },
        "tenant-b-researcher": {
            "subject": "bob@example.com",
            "tenant_id": "tenant-b",
            "roles": ["RESEARCHER"],
        },
        "tenant-a-reviewer": {
            "subject": "reviewer@example.com",
            "tenant_id": "tenant-a",
            "roles": ["REVIEWER"],
        },
    }
    return Authenticator(mode="api_key", api_keys_json=json.dumps(identities))


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.asyncio
async def test_workbench_is_a_public_product_entrypoint() -> None:
    transport = httpx.ASGITransport(
        app=create_app(build_default_workflow(), authenticator=_authenticator())
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/workbench")
        library = await client.get("/library")
        admin = await client.get("/admin")
        asset_path = re.search(r'<script[^>]+src="([^"]+\.js)"', page.text)
        assert asset_path is not None
        asset = await client.get(asset_path.group(1))
        protected = await client.get("/v1/research/runs/unknown")

    assert page.status_code == 200
    assert '<div id="root"></div>' in page.text
    assert "Atlas Research" in page.text
    assert library.text == page.text
    assert admin.text == page.text
    assert asset.status_code == 200
    assert "javascript" in asset.headers["content-type"]
    assert protected.status_code == 401


@pytest.mark.asyncio
async def test_api_key_rbac_tenant_isolation_and_audit() -> None:
    app = create_app(build_default_workflow(), authenticator=_authenticator())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/research/runs",
            headers={**_headers("tenant-a-admin"), "Idempotency-Key": "same-key"},
            json={"question": "How should claims be verified in research agents?"},
        )
        second = await client.post(
            "/v1/research/runs",
            headers={**_headers("tenant-b-researcher"), "Idempotency-Key": "same-key"},
            json={"question": "How should claims be verified in research agents?"},
        )
        forbidden = await client.post(
            "/v1/research/runs",
            headers=_headers("tenant-a-reviewer"),
            json={"question": "How should claims be reviewed by a human?"},
        )
        hidden = await client.get(
            f"/v1/research/runs/{first.json()['run_id']}",
            headers=_headers("tenant-b-researcher"),
        )
        audit = await client.get("/v1/audit/events", headers=_headers("tenant-a-admin"))
        mcp = await client.get("/v1/integrations/mcp", headers=_headers("tenant-a-admin"))
        forbidden_mcp = await client.get(
            "/v1/integrations/mcp",
            headers=_headers("tenant-b-researcher"),
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["run_id"] != second.json()["run_id"]
    assert forbidden.status_code == 403
    assert hidden.status_code == 404
    assert audit.status_code == 200
    assert audit.json()[0]["actor"] == "alice@example.com"
    assert audit.json()[0]["action"] == "research.submitted"
    assert mcp.status_code == 200
    assert mcp.json() == []
    assert forbidden_mcp.status_code == 403


@pytest.mark.asyncio
async def test_idempotent_retry_is_allowed_when_active_quota_is_full() -> None:
    store = InMemoryRunStore()
    service = ResearchApplicationService(
        build_default_workflow(),
        store,
        queue=_HoldingQueue(),  # type: ignore[arg-type]
        quota=QuotaPolicy(store, TenantQuota(max_active_runs=1)),
    )
    request = ResearchRequest(question="How should research agents verify claims?")
    try:
        first = await service.submit(request, idempotency_key="retry", tenant_id="tenant-a")
        retry = await service.submit(request, idempotency_key="retry", tenant_id="tenant-a")
        assert retry.run_id == first.run_id
        with pytest.raises(QuotaExceededError):
            await service.submit(request, tenant_id="tenant-a")
    finally:
        await service.close()
