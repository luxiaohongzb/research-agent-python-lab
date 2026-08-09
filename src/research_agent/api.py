from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import PlainTextResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from research_agent.application import (
    IdempotencyConflictError,
    ResearchApplicationService,
    ReviewValidationError,
    RunNotFoundError,
)
from research_agent.citations import render_bibtex, render_csl_json
from research_agent.config import get_settings
from research_agent.domain import (
    HumanReviewRequest,
    ResearchRequest,
    ResearchResult,
    RunSnapshot,
)
from research_agent.events import InMemoryRunEventBroker
from research_agent.ingestion import GrobidClient, GrobidError, TeiParser
from research_agent.observability import RuntimeObservability, RuntimeSummary
from research_agent.run_store import PostgresRunStore, RunStore
from research_agent.workflow import ResearchWorkflow, build_default_workflow


class IngestionResult(BaseModel):
    paper_id: str
    title: str
    passages_indexed: int
    citation_edges_indexed: int
    document_hash: str
    parser: str
    parser_version: str


def create_app(
    workflow: ResearchWorkflow | None = None,
    *,
    grobid_client: GrobidClient | None = None,
    tei_parser: TeiParser | None = None,
    run_store: RunStore | None = None,
    event_broker: InMemoryRunEventBroker | None = None,
    observability: RuntimeObservability | None = None,
) -> FastAPI:
    settings = get_settings()
    active_workflow = workflow or build_default_workflow(settings)
    active_store = run_store
    if active_store is None and settings.run_store_mode == "postgres":
        active_store = PostgresRunStore(settings.database_url)
    runtime_observability = observability or RuntimeObservability(
        otel_enabled=settings.otel_enabled,
        service_name=settings.otel_service_name,
    )
    service = ResearchApplicationService(
        active_workflow,
        active_store,
        events=event_broker,
        observability=runtime_observability,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await active_workflow.initialize()
        try:
            yield
        finally:
            await service.close()

    api = FastAPI(
        title="Research Agent Python Lab",
        version="0.1.0",
        description="Evidence-first and claim-verifiable intelligent research assistant",
        lifespan=lifespan,
    )
    api.state.research_service = service
    api.mount("/metrics", make_asgi_app())
    grobid = grobid_client or GrobidClient(
        base_url=settings.grobid_url,
        timeout_seconds=settings.grobid_timeout_seconds,
    )
    parser = tei_parser or TeiParser()

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.post("/v1/research/run", response_model=ResearchResult)
    async def run_research(request: ResearchRequest) -> ResearchResult:
        return await service.run(request)

    @api.post(
        "/v1/research/runs",
        response_model=RunSnapshot,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_research(
        request: ResearchRequest,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=200),
        ] = None,
    ) -> RunSnapshot:
        try:
            return await service.submit(request, idempotency_key=idempotency_key)
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/v1/research/runs/{run_id}", response_model=RunSnapshot)
    async def get_research(run_id: str) -> RunSnapshot:
        try:
            return await service.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc

    @api.delete("/v1/research/runs/{run_id}", response_model=RunSnapshot)
    async def cancel_research(run_id: str) -> RunSnapshot:
        try:
            return await service.cancel(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc

    @api.post("/v1/research/runs/{run_id}/resume", response_model=RunSnapshot)
    async def resume_research(run_id: str) -> RunSnapshot:
        try:
            return await service.resume(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc

    @api.get(
        "/v1/research/runs/{run_id}/events",
        response_class=EventSourceResponse,
    )
    async def stream_research_events(
        run_id: str,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
    ) -> AsyncIterator[ServerSentEvent]:
        try:
            await service.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc

        async for item in service.stream_events(run_id, after_sequence=after_sequence):
            yield ServerSentEvent(
                data=item.model_dump(mode="json"),
                event=item.event,
                id=str(item.sequence),
                retry=3_000,
            )

    @api.post("/v1/research/runs/{run_id}/reviews", response_model=RunSnapshot)
    async def review_research(run_id: str, request: HumanReviewRequest) -> RunSnapshot:
        try:
            return await service.review(run_id, request)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc
        except ReviewValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @api.get("/v1/research/runs/{run_id}/export/bibtex", response_class=PlainTextResponse)
    async def export_bibtex(run_id: str) -> PlainTextResponse:
        try:
            snapshot = await service.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc
        if snapshot.result is None:
            raise HTTPException(status_code=409, detail="Research run is not complete")
        return PlainTextResponse(
            render_bibtex(snapshot.result.papers),
            media_type="application/x-bibtex",
        )

    @api.get("/v1/research/runs/{run_id}/export/csl-json")
    async def export_csl(run_id: str) -> list[dict[str, object]]:
        try:
            snapshot = await service.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc
        if snapshot.result is None:
            raise HTTPException(status_code=409, detail="Research run is not complete")
        return render_csl_json(snapshot.result.papers)

    @api.get("/v1/metrics/summary", response_model=RuntimeSummary)
    async def metrics_summary() -> RuntimeSummary:
        return await service.metrics_summary()

    @api.post(
        "/v1/corpus/documents",
        response_model=IngestionResult,
        status_code=status.HTTP_201_CREATED,
    )
    async def ingest_document(file: Annotated[UploadFile, File()]) -> IngestionResult:
        if file.content_type not in {"application/pdf", "application/x-pdf"}:
            raise HTTPException(status_code=415, detail="Only PDF documents are supported")
        pdf_bytes = await file.read(30 * 1024 * 1024 + 1)
        try:
            tei = await grobid.process_pdf(pdf_bytes, filename=file.filename or "document.pdf")
            document = parser.parse(tei)
            await active_workflow.ingest(document)
        except GrobidError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GROBID service is unavailable") from exc
        return IngestionResult(
            paper_id=document.paper.paper_id,
            title=document.paper.title,
            passages_indexed=len(document.passages),
            citation_edges_indexed=len(document.citation_edges),
            document_hash=document.document_hash,
            parser=document.parser,
            parser_version=document.parser_version,
        )

    return api


app = create_app()
