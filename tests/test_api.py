from pathlib import Path

import httpx
import pytest

from research_agent.api import create_app
from research_agent.ingestion import GrobidClient
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


@pytest.mark.asyncio
async def test_pdf_ingestion_endpoint_indexes_structured_passages() -> None:
    tei = (Path(__file__).parent / "fixtures" / "sample.tei.xml").read_bytes()
    grobid_http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=tei))
    )
    grobid = GrobidClient(base_url="http://grobid", client=grobid_http)
    transport = httpx.ASGITransport(app=create_app(build_default_workflow(), grobid_client=grobid))
    async with (
        grobid_http,
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        response = await client.post(
            "/v1/corpus/documents",
            files={"file": ("paper.pdf", b"%PDF-1.7 fixture", "application/pdf")},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["title"] == "Evidence-Grounded Scientific Agents"
    assert payload["passages_indexed"] >= 4
    assert payload["citation_edges_indexed"] == 1
