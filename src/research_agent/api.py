from __future__ import annotations

import secrets
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
from research_agent.audit import AuditEvent, AuditStore, InMemoryAuditStore, PostgresAuditStore
from research_agent.auth import (
    Authenticator,
    Permission,
    Principal,
    TokenManager,
    require_permission,
)
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
from research_agent.identity import (
    ChangePasswordRequest,
    CreateUserRequest,
    DuplicateUserError,
    IdentityService,
    InMemoryUserStore,
    InvalidCredentialsError,
    LoginRequest,
    LogoutRequest,
    PostgresUserStore,
    RefreshRequest,
    ResetPasswordRequest,
    RoleView,
    TokenPair,
    UnsafeUserChangeError,
    UpdateUserRequest,
    UserNotFoundError,
    UserStore,
    UserView,
    role_catalog,
)
from research_agent.ingestion import GrobidClient, GrobidError, TeiParser
from research_agent.mcp_client import (
    McpServerStatus,
    parse_mcp_paper_servers,
    probe_mcp_paper_servers,
)
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
    user_store: UserStore | None = None,
    identity_service: IdentityService | None = None,
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
    if active_audit is None:
        active_audit = (
            PostgresAuditStore(settings.database_url)
            if settings.run_store_mode == "postgres"
            else InMemoryAuditStore()
        )
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

    configured_secret = (
        settings.auth_token_secret.get_secret_value() if settings.auth_token_secret else None
    )
    if settings.auth_mode in {"rbac", "hybrid"} and not configured_secret:
        raise RuntimeError(
            "RBAC authentication requires RESEARCH_AGENT_AUTH_TOKEN_SECRET with at least 32 bytes"
        )
    tokens = TokenManager(
        configured_secret or secrets.token_urlsafe(48),
        issuer=settings.auth_issuer,
        audience=settings.auth_audience,
        access_ttl_seconds=settings.auth_access_token_seconds,
    )
    active_user_store = user_store
    if active_user_store is None:
        postgres_identity = settings.identity_store_mode == "postgres" or (
            settings.identity_store_mode == "auto" and settings.run_store_mode == "postgres"
        )
        active_user_store = (
            PostgresUserStore(settings.database_url) if postgres_identity else InMemoryUserStore()
        )
    identity = identity_service or IdentityService(
        active_user_store,
        tokens,
        audit=active_audit,
        refresh_ttl_seconds=settings.auth_refresh_token_seconds,
        max_failed_attempts=settings.auth_max_failed_attempts,
        lockout_seconds=settings.auth_lockout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await identity.initialize(
            bootstrap_tenant_id=settings.bootstrap_admin_tenant_id,
            bootstrap_email=settings.bootstrap_admin_email,
            bootstrap_password=(
                settings.bootstrap_admin_password.get_secret_value()
                if settings.bootstrap_admin_password
                else None
            ),
            bootstrap_display_name=settings.bootstrap_admin_display_name,
        )
        await service.initialize()
        try:
            yield
        finally:
            await identity.close()
            await service.close()

    api = FastAPI(
        title="Research Agent Python Lab",
        version="1.2.0",
        description="Evidence-first and claim-verifiable intelligent research assistant",
        lifespan=lifespan,
    )
    api.state.research_service = service
    active_auth = authenticator or Authenticator(
        mode=settings.auth_mode,
        api_keys_json=settings.api_keys_json,
        token_manager=tokens,
        principal_resolver=identity,
    )
    api.state.authenticator = active_auth
    api.state.identity_service = identity
    api.mount("/metrics", make_asgi_app())
    static_dir = Path(__file__).with_name("static")
    api.mount("/assets", StaticFiles(directory=static_dir), name="assets")
    react_dir = static_dir / "react"
    react_index = react_dir / "index.html"
    if react_index.exists():
        api.mount("/ui", StaticFiles(directory=react_dir), name="react-ui")
    grobid = grobid_client or GrobidClient(
        base_url=settings.grobid_url,
        timeout_seconds=settings.grobid_timeout_seconds,
    )
    parser = tei_parser or TeiParser()

    @api.post("/v1/auth/login", response_model=TokenPair)
    async def login(request: LoginRequest) -> TokenPair:
        try:
            return await identity.login(request)
        except InvalidCredentialsError as exc:
            raise HTTPException(
                status_code=401,
                detail="Invalid email, password, or account state",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    @api.post("/v1/auth/refresh", response_model=TokenPair)
    async def refresh_access(request: RefreshRequest) -> TokenPair:
        try:
            return await identity.refresh(request.refresh_token)
        except (InvalidCredentialsError, UserNotFoundError) as exc:
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired refresh token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    @api.post("/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(request: LogoutRequest) -> None:
        try:
            await identity.logout(request.refresh_token, actor="refresh-session")
        except InvalidCredentialsError:
            return None

    @api.get("/v1/auth/me", response_model=UserView)
    async def current_user(principal: PrincipalDependency) -> UserView:
        return await identity.me(principal)

    @api.post("/v1/auth/change-password", status_code=status.HTTP_204_NO_CONTENT)
    async def change_password(
        request: ChangePasswordRequest, principal: PrincipalDependency
    ) -> None:
        try:
            await identity.change_password(principal, request)
        except InvalidCredentialsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except UnsafeUserChangeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/v1/roles", response_model=list[RoleView])
    async def list_roles(principal: PrincipalDependency) -> tuple[RoleView, ...]:
        require_permission(principal, Permission.ROLES_READ)
        return role_catalog()

    @api.get("/v1/users", response_model=list[UserView])
    async def list_users(
        principal: PrincipalDependency,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> tuple[UserView, ...]:
        require_permission(principal, Permission.USERS_READ)
        return await identity.list_users(principal.tenant_id, limit=limit, offset=offset)

    @api.post("/v1/users", response_model=UserView, status_code=status.HTTP_201_CREATED)
    async def create_user(request: CreateUserRequest, principal: PrincipalDependency) -> UserView:
        require_permission(principal, Permission.USERS_WRITE)
        try:
            return await identity.create_user(
                tenant_id=principal.tenant_id,
                request=request,
                actor=principal.subject,
            )
        except DuplicateUserError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/v1/users/{user_id}", response_model=UserView)
    async def get_user(user_id: str, principal: PrincipalDependency) -> UserView:
        require_permission(principal, Permission.USERS_READ)
        try:
            return await identity.get_user(user_id, tenant_id=principal.tenant_id)
        except UserNotFoundError as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc

    @api.patch("/v1/users/{user_id}", response_model=UserView)
    async def update_user(
        user_id: str, request: UpdateUserRequest, principal: PrincipalDependency
    ) -> UserView:
        require_permission(principal, Permission.USERS_WRITE)
        try:
            return await identity.update_user(
                user_id,
                tenant_id=principal.tenant_id,
                request=request,
                actor=principal,
            )
        except UserNotFoundError as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc
        except UnsafeUserChangeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/v1/users/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
    async def reset_user_password(
        user_id: str,
        request: ResetPasswordRequest,
        principal: PrincipalDependency,
    ) -> None:
        require_permission(principal, Permission.USERS_WRITE)
        try:
            await identity.reset_password(
                user_id,
                tenant_id=principal.tenant_id,
                new_password=request.new_password,
                actor=principal.subject,
            )
        except UserNotFoundError as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc

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
        return FileResponse(react_index if react_index.exists() else static_dir / "workbench.html")

    @api.get("/library", include_in_schema=False)
    async def library() -> FileResponse:
        return FileResponse(react_index if react_index.exists() else static_dir / "workbench.html")

    @api.get("/admin", include_in_schema=False)
    async def admin_console() -> FileResponse:
        return FileResponse(react_index if react_index.exists() else static_dir / "workbench.html")

    @api.post("/v1/research/run", response_model=ResearchResult)
    async def run_research(
        request: ResearchRequest, principal: PrincipalDependency
    ) -> ResearchResult:
        require_permission(principal, Permission.RESEARCH_CREATE)
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
        require_permission(principal, Permission.RESEARCH_CREATE)
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
        require_permission(principal, Permission.RESEARCH_READ)
        try:
            return await service.get(run_id, tenant_id=principal.tenant_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc

    @api.delete("/v1/research/runs/{run_id}", response_model=RunSnapshot)
    async def cancel_research(run_id: str, principal: PrincipalDependency) -> RunSnapshot:
        require_permission(principal, Permission.RESEARCH_CANCEL)
        try:
            return await service.cancel(
                run_id, tenant_id=principal.tenant_id, actor=principal.subject
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc

    @api.post("/v1/research/runs/{run_id}/resume", response_model=RunSnapshot)
    async def resume_research(run_id: str, principal: PrincipalDependency) -> RunSnapshot:
        require_permission(principal, Permission.RESEARCH_CANCEL)
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
        require_permission(principal, Permission.RESEARCH_READ)
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
        require_permission(principal, Permission.RESEARCH_REVIEW)
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
        require_permission(principal, Permission.RESEARCH_EXPORT)
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
        require_permission(principal, Permission.RESEARCH_EXPORT)
        try:
            snapshot = await service.get(run_id, tenant_id=principal.tenant_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc
        if snapshot.result is None:
            raise HTTPException(status_code=409, detail="Research run is not complete")
        return render_csl_json(snapshot.result.papers)

    @api.get("/v1/metrics/summary", response_model=RuntimeSummary)
    async def metrics_summary(principal: PrincipalDependency) -> RuntimeSummary:
        require_permission(principal, Permission.METRICS_READ)
        return await service.metrics_summary()

    @api.get("/v1/audit/events", response_model=list[AuditEvent])
    async def audit_events(
        principal: PrincipalDependency,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> tuple[AuditEvent, ...]:
        require_permission(principal, Permission.AUDIT_READ)
        return await service.audit_events(principal.tenant_id, limit=limit)

    @api.get("/v1/operations/dead-letters", response_model=list[DeadLetter])
    async def dead_letters(
        principal: PrincipalDependency,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> tuple[DeadLetter, ...]:
        require_permission(principal, Permission.OPERATIONS_MANAGE)
        if active_queue is None:
            return ()
        return await active_queue.dead_letters(limit=limit)

    @api.get("/v1/integrations/mcp", response_model=list[McpServerStatus])
    async def mcp_integrations(principal: PrincipalDependency) -> tuple[McpServerStatus, ...]:
        require_permission(principal, Permission.INTEGRATIONS_READ)
        if not settings.mcp_enabled:
            return ()
        configs = parse_mcp_paper_servers(settings.mcp_paper_servers_json)
        return await probe_mcp_paper_servers(
            configs,
            timeout_seconds=settings.mcp_timeout_seconds,
        )

    @api.post(
        "/v1/corpus/documents",
        response_model=IngestionResult,
        status_code=status.HTTP_201_CREATED,
    )
    async def ingest_document(
        principal: PrincipalDependency,
        file: Annotated[UploadFile, File()],
    ) -> IngestionResult:
        require_permission(principal, Permission.CORPUS_WRITE)
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
