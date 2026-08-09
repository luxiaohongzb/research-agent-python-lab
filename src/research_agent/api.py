from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from research_agent.application import (
    IdempotencyConflictError,
    ResearchApplicationService,
    ReviewValidationError,
    RunNotFoundError,
)
from research_agent.audit import AuditEvent, AuditStore, PostgresAuditStore
from research_agent.auth import Authenticator, Principal, Role, require_role
from research_agent.citations import render_bibtex, render_csl_json
from research_agent.config import get_settings
from research_agent.distributed import (
    CancellationRegistry,
    DeadLetter,
    RedisCancellationRegistry,
    RedisRunQueue,
    RunQueue,
)
from research_agent.domain import (
    HumanReviewRequest,
    ResearchRequest,
    ResearchResult,
    RunSnapshot,
)
from research_agent.events import InMemoryRunEventBroker, RedisRunEventBroker, RunEventBroker
from research_agent.governance import QuotaExceededError, QuotaPolicy, TenantQuota
from research_agent.ingestion import GrobidClient, GrobidError, TeiParser
from research_agent.observability import RuntimeObservability, RuntimeSummary
from research_agent.run_store import InMemoryRunStore, PostgresRunStore, RunStore
from research_agent.workflow import ResearchWorkflow, build_default_workflow


class IngestionResult(BaseModel):
    paper_id: str
    title: str
    passages_indexed: int
    citation_edges_indexed: int
    document_hash: str
    parser: str
    parser_version: str


async def _current_principal(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> Principal:
    authenticator: Authenticator = request.app.state.authenticator
    return await authenticator.authenticate(authorization)


PrincipalDependency = Annotated[Principal, Depends(_current_principal)]


def create_app(
    workflow: ResearchWorkflow | None = None,
    *,
    grobid_client: GrobidClient | None = None,
    tei_parser: TeiParser | None = None,
    run_store: RunStore | None = None,
    event_broker: RunEventBroker | None = None,
    run_queue: RunQueue | None = None,
    cancellations: CancellationRegistry | None = None,
    observability: RuntimeObservability | None = None,
    audit_store: AuditStore | None = None,
    authenticator: Authenticator | None = None,
) -> FastAPI:
    settings = get_settings()
    active_workflow = workflow or build_default_workflow(settings)
    active_store = run_store
    if active_store is None:
        active_store = (
            PostgresRunStore(settings.database_url)
            if settings.run_store_mode == "postgres"
            else InMemoryRunStore()
        )
    active_events = event_broker
    if active_events is None:
        if settings.event_broker_mode == "redis":
            active_events = RedisRunEventBroker(
                settings.redis_url,
                prefix=settings.redis_prefix,
            )
        else:
            active_events = InMemoryRunEventBroker()
    active_queue = run_queue
    if active_queue is None and settings.dispatch_mode == "redis":
        active_queue = RedisRunQueue(
            settings.redis_url,
            consumer_name="api-dispatcher",
            group=settings.redis_consumer_group,
            prefix=settings.redis_prefix,
            lease_seconds=settings.queue_lease_seconds,
        )
    active_cancellations = cancellations
    if active_cancellations is None and (
        settings.cancellation_mode == "redis" or settings.dispatch_mode == "redis"
    ):
        active_cancellations = RedisCancellationRegistry(
            settings.redis_url,
            prefix=settings.redis_prefix,
        )
    runtime_observability = observability or RuntimeObservability(
        otel_enabled=settings.otel_enabled,
        service_name=settings.otel_service_name,
    )
    active_audit = audit_store
    if active_audit is None and settings.run_store_mode == "postgres":
        active_audit = PostgresAuditStore(settings.database_url)
    quota = QuotaPolicy(
        active_store,
        TenantQuota(
            max_active_runs=settings.tenant_max_active_runs,
            max_workers=settings.tenant_max_workers,
            max_total_tokens=settings.tenant_max_total_tokens,
            max_cost_usd=settings.tenant_max_cost_usd,
        ),
    )
    service = ResearchApplicationService(
        active_workflow,
        active_store,
        events=active_events,
        queue=active_queue,
        cancellations=active_cancellations,
        observability=runtime_observability,
        audit=active_audit,
        quota=quota,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await service.initialize()
        try:
            yield
        finally:
            await service.close()

    api = FastAPI(
        title="Research Agent Python Lab",
        version="1.0.0",
        description="Evidence-first and claim-verifiable intelligent research assistant",
        lifespan=lifespan,
    )
    api.state.research_service = service
    active_auth = authenticator or Authenticator(
        mode=settings.auth_mode,
        api_keys_json=settings.api_keys_json,
    )
    api.state.authenticator = active_auth
    api.mount("/metrics", make_asgi_app())
    static_dir = Path(__file__).with_name("static")
    api.mount("/assets", StaticFiles(directory=static_dir), name="assets")
    grobid = grobid_client or GrobidClient(
        base_url=settings.grobid_url,
        timeout_seconds=settings.grobid_timeout_seconds,
    )
    parser = tei_parser or TeiParser()

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/ready")
    async def ready() -> dict[str, str]:
        await active_store.count_active("__readiness__")
        return {"status": "ready"}

    @api.get("/", include_in_schema=False)
    async def home() -> RedirectResponse:
        return RedirectResponse("/workbench")

    @api.get("/workbench", include_in_schema=False)
    async def workbench() -> FileResponse:
        return FileResponse(static_dir / "workbench.html")

    @api.post("/v1/research/run", response_model=ResearchResult)
    async def run_research(
        request: ResearchRequest, principal: PrincipalDependency
    ) -> ResearchResult:
        require_role(principal, Role.RESEARCHER, Role.ADMIN)
        try:
            return await service.run(
                request,
                tenant_id=principal.tenant_id,
                actor=principal.subject,
            )
        except QuotaExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc

    @api.post(
        "/v1/research/runs",
        response_model=RunSnapshot,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_research(
        request: ResearchRequest,
        principal: PrincipalDependency,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=200),
        ] = None,
    ) -> RunSnapshot:
        require_role(principal, Role.RESEARCHER, Role.ADMIN)
        try:
            return await service.submit(
                request,
                idempotency_key=idempotency_key,
                tenant_id=principal.tenant_id,
                actor=principal.subject,
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except QuotaExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc

    @api.get("/v1/research/runs/{run_id}", response_model=RunSnapshot)
    async def get_research(run_id: str, principal: PrincipalDependency) -> RunSnapshot:
        require_role(principal, Role.RESEARCHER, Role.REVIEWER, Role.ADMIN)
        try:
            return await service.get(run_id, tenant_id=principal.tenant_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc

    @api.delete("/v1/research/runs/{run_id}", response_model=RunSnapshot)
    async def cancel_research(run_id: str, principal: PrincipalDependency) -> RunSnapshot:
        require_role(principal, Role.RESEARCHER, Role.ADMIN)
        try:
            return await service.cancel(
                run_id, tenant_id=principal.tenant_id, actor=principal.subject
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc

    @api.post("/v1/research/runs/{run_id}/resume", response_model=RunSnapshot)
    async def resume_research(run_id: str, principal: PrincipalDependency) -> RunSnapshot:
        require_role(principal, Role.RESEARCHER, Role.ADMIN)
        try:
            return await service.resume(
                run_id, tenant_id=principal.tenant_id, actor=principal.subject
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc

    @api.get(
        "/v1/research/runs/{run_id}/events",
        response_class=EventSourceResponse,
    )
    async def stream_research_events(
        run_id: str,
        principal: PrincipalDependency,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
    ) -> AsyncIterator[ServerSentEvent]:
        require_role(principal, Role.RESEARCHER, Role.REVIEWER, Role.ADMIN)
        try:
            await service.get(run_id, tenant_id=principal.tenant_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc

        async for item in service.stream_events(
            run_id,
            after_sequence=after_sequence,
            tenant_id=principal.tenant_id,
        ):
            yield ServerSentEvent(
                data=item.model_dump(mode="json"),
                event=item.event,
                id=str(item.sequence),
                retry=3_000,
            )

    @api.post("/v1/research/runs/{run_id}/reviews", response_model=RunSnapshot)
    async def review_research(
        run_id: str, request: HumanReviewRequest, principal: PrincipalDependency
    ) -> RunSnapshot:
        require_role(principal, Role.REVIEWER, Role.ADMIN)
        authoritative_request = request.model_copy(update={"reviewer": principal.subject})
        try:
            return await service.review(
                run_id,
                authoritative_request,
                tenant_id=principal.tenant_id,
                actor=principal.subject,
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc
        except ReviewValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @api.get("/v1/research/runs/{run_id}/export/bibtex", response_class=PlainTextResponse)
    async def export_bibtex(run_id: str, principal: PrincipalDependency) -> PlainTextResponse:
        require_role(principal, Role.RESEARCHER, Role.REVIEWER, Role.ADMIN)
        try:
            snapshot = await service.get(run_id, tenant_id=principal.tenant_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc
        if snapshot.result is None:
            raise HTTPException(status_code=409, detail="Research run is not complete")
        return PlainTextResponse(
            render_bibtex(snapshot.result.papers),
            media_type="application/x-bibtex",
        )

    @api.get("/v1/research/runs/{run_id}/export/csl-json")
    async def export_csl(run_id: str, principal: PrincipalDependency) -> list[dict[str, object]]:
        require_role(principal, Role.RESEARCHER, Role.REVIEWER, Role.ADMIN)
        try:
            snapshot = await service.get(run_id, tenant_id=principal.tenant_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc
        if snapshot.result is None:
            raise HTTPException(status_code=409, detail="Research run is not complete")
        return render_csl_json(snapshot.result.papers)

    @api.get("/v1/metrics/summary", response_model=RuntimeSummary)
    async def metrics_summary(principal: PrincipalDependency) -> RuntimeSummary:
        require_role(principal, Role.ADMIN)
        return await service.metrics_summary()

    @api.get("/v1/audit/events", response_model=list[AuditEvent])
    async def audit_events(
        principal: PrincipalDependency,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> tuple[AuditEvent, ...]:
        require_role(principal, Role.ADMIN)
        return await service.audit_events(principal.tenant_id, limit=limit)

    @api.get("/v1/operations/dead-letters", response_model=list[DeadLetter])
    async def dead_letters(
        principal: PrincipalDependency,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> tuple[DeadLetter, ...]:
        require_role(principal, Role.ADMIN)
        if active_queue is None:
            return ()
        return await active_queue.dead_letters(limit=limit)

    @api.post(
        "/v1/corpus/documents",
        response_model=IngestionResult,
        status_code=status.HTTP_201_CREATED,
    )
    async def ingest_document(
        principal: PrincipalDependency,
        file: Annotated[UploadFile, File()],
    ) -> IngestionResult:
        require_role(principal, Role.RESEARCHER, Role.ADMIN)
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
