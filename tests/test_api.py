import httpx
import pytest

from research_agent.api import create_app
from research_agent.workflow import build_default_workflow


@pytest.mark.asyncio
async def test_health_and_synchronous_research_api() -> None:
    transport = httpx.ASGITransport(app=create_app(build_default_workflow()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        response = await client.post(
            "/v1/research/run",
            json={"question": "How do agentic RAG and claim verification work together?"},
        )

    assert health.json() == {"status": "ok"}
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "COMPLETED"
    assert payload["claims"]
    assert all(item["status"] == "SUPPORTED" for item in payload["verifications"])


@pytest.mark.asyncio
async def test_unknown_run_returns_404() -> None:
    transport = httpx.ASGITransport(app=create_app(build_default_workflow()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/research/runs/not-found")

    assert response.status_code == 404
