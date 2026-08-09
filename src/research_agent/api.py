from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from research_agent.application import ResearchApplicationService, RunNotFoundError
from research_agent.config import get_settings
from research_agent.domain import ResearchRequest, ResearchResult, RunSnapshot
from research_agent.ingestion import GrobidClient, GrobidError, TeiParser
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
) -> FastAPI:
    settings = get_settings()
    api = FastAPI(
        title="Research Agent Python Lab",
        version="0.1.0",
        description="Evidence-first and claim-verifiable intelligent research assistant",
    )
    active_workflow = workflow or build_default_workflow(settings)
    service = ResearchApplicationService(active_workflow)
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
    async def submit_research(request: ResearchRequest) -> RunSnapshot:
        return await service.submit(request)

    @api.get("/v1/research/runs/{run_id}", response_model=RunSnapshot)
    async def get_research(run_id: str) -> RunSnapshot:
        try:
            return await service.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc

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
