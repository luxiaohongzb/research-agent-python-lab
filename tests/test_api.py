import asyncio
from pathlib import Path

import httpx
import pytest

from research_agent.api import create_app
from research_agent.ingestion import GrobidClient
from research_agent.workflow import build_default_workflow


async def _wait_for_api_run(client: httpx.AsyncClient, run_id: str) -> dict[str, object]:
    for _ in range(100):
        response = await client.get(f"/v1/research/runs/{run_id}")
        payload = response.json()
        if payload["status"] in {"COMPLETED", "NEEDS_REVIEW", "FAILED", "CANCELLED"}:
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError("API run did not complete")


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
async def test_deep_research_api_exposes_bounded_worker_results() -> None:
    transport = httpx.ASGITransport(app=create_app(build_default_workflow()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/research/run",
            json={
                "question": "Compare agentic RAG and claim verification approaches",
                "max_iterations": 1,
                "max_workers": 2,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["workers"]) == 3
    assert payload["budget"]["used_workers"] == 3
    assert all(item["artifact_ref"]["artifact_id"] for item in payload["workers"])


@pytest.mark.asyncio
async def test_async_workbench_events_review_exports_and_metrics() -> None:
    app = create_app(build_default_workflow())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        request = {"question": "How do agentic RAG and claim verification work together?"}
        first = await client.post(
            "/v1/research/runs",
            json=request,
            headers={"Idempotency-Key": "api-idempotency-test"},
        )
        second = await client.post(
            "/v1/research/runs",
            json=request,
            headers={"Idempotency-Key": "api-idempotency-test"},
        )
        run_id = first.json()["run_id"]
        assert second.json()["run_id"] == run_id
        conflict = await client.post(
            "/v1/research/runs",
            json={"question": "How should evidence be reviewed by humans?"},
            headers={"Idempotency-Key": "api-idempotency-test"},
        )
        assert conflict.status_code == 409
        snapshot = await _wait_for_api_run(client, run_id)
        assert snapshot["result"] is not None

        events = await client.get(f"/v1/research/runs/{run_id}/events")
        result = snapshot["result"]
        assert isinstance(result, dict)
        review = await client.post(
            f"/v1/research/runs/{run_id}/reviews",
            json={
                "reviewer": "interviewer@example.com",
                "decision": "APPROVE",
                "claim_id": result["claims"][0]["claim_id"],
                "evidence_id": result["evidence"][0]["evidence_id"],
                "comment": "Evidence and claim are aligned.",
            },
        )
        bibtex = await client.get(f"/v1/research/runs/{run_id}/export/bibtex")
        csl = await client.get(f"/v1/research/runs/{run_id}/export/csl-json")
        summary = await client.get("/v1/metrics/summary")
        prometheus = await client.get("/metrics/")

    assert "event: progress" in events.text
    assert "event: completed" in events.text
    assert review.status_code == 200
    assert review.json()["reviews"][0]["decision"] == "APPROVE"
    assert "@article{" in bibtex.text
    assert csl.json()[0]["type"] == "article-journal"
    assert summary.json()["total_runs"] >= 1
    assert "research_agent_runs_total" in prometheus.text


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
