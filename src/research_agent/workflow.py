from __future__ import annotations

import asyncio
import operator
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, TypedDict, TypeVar, cast
from uuid import uuid4

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from research_agent.artifacts import (
    ArtifactNotFoundError,
    ArtifactStore,
    InMemoryArtifactStore,
    S3ArtifactStore,
)
from research_agent.config import Settings, get_settings
from research_agent.domain import (
    ArtifactKind,
    ArtifactRef,
    AtomicClaim,
    Complexity,
    EvidenceCard,
    ModelInvocation,
    Paper,
    ParsedDocument,
    Passage,
    ResearchBudget,
    ResearchPlan,
    ResearchReport,
    ResearchRequest,
    ResearchResult,
    ResearchWorkerAssignment,
    ResearchWorkerResult,
    RunStatus,
    SearchTask,
    SourceScope,
    TraceEvent,
    VerificationResult,
    VerificationStatus,
    WorkerStatus,
)
from research_agent.providers import (
    CompositePaperProvider,
    CrossrefPaperProvider,
    OfflinePaperProvider,
    OpenAlexPaperProvider,
    deduplicate_papers,
)
from research_agent.reasoner import DeterministicReasoner, Reasoned, ResearchReasoner
from research_agent.retrieval import (
    DiversityReranker,
    HashEmbeddingModel,
    InMemoryHybridIndex,
    ResearchRetriever,
    RetrievalBatch,
    SentenceTransformerReranker,
)


class ResearchState(TypedDict, total=False):
    run_id: str
    request: dict[str, Any]
    status: str
    plan: dict[str, Any]
    budget: dict[str, Any]
    pending_tasks: list[dict[str, Any]]
    raw_papers: list[dict[str, Any]]
    raw_passages: list[dict[str, Any]]
    papers: list[dict[str, Any]]
    passages: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    verifications: list[dict[str, Any]]
    report: dict[str, Any]
    coverage_score: float
    iteration: int
    trace: list[dict[str, Any]]
    warnings: list[str]
    model_invocations: list[dict[str, Any]]
    started_epoch_seconds: float
    worker_assignments: list[dict[str, Any]]
    worker_outputs: Annotated[list[dict[str, Any]], operator.add]
    worker_results: list[dict[str, Any]]


class WorkerState(TypedDict):
    worker_assignment: dict[str, Any]
    worker_outputs: Annotated[list[dict[str, Any]], operator.add]


ReasonedValueT = TypeVar("ReasonedValueT")


class ResearchWorkflow:
    def __init__(
        self,
        *,
        provider: CompositePaperProvider | None = None,
        retriever: ResearchRetriever | None = None,
        reasoner: ResearchReasoner | None = None,
        artifact_store: ArtifactStore | None = None,
        worker_timeout_seconds: float = 45.0,
        model_max_concurrency: int = 1,
        checkpoint_dsn: str | None = None,
    ) -> None:
        if retriever is None:
            if provider is None:
                raise ValueError("provider or retriever is required")
            retriever = ResearchRetriever(metadata=provider)
        self._retriever = retriever
        self._baseline_reasoner = DeterministicReasoner()
        self._reasoner = reasoner or self._baseline_reasoner
        self._artifact_store = artifact_store or InMemoryArtifactStore()
        self._worker_timeout_seconds = worker_timeout_seconds
        self._model_max_concurrency = max(1, model_max_concurrency)
        self._checkpointer = InMemorySaver()
        self._checkpoint_dsn = checkpoint_dsn
        self._checkpoint_context: Any = None
        self._initialization_lock = asyncio.Lock()
        self._initialized = checkpoint_dsn is None
        self.graph = self._build_graph()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            if type(asyncio.get_running_loop()).__name__ == "ProactorEventLoop":
                raise RuntimeError(
                    "PostgreSQL checkpointing requires a Windows selector event loop; "
                    "start the API with `research-agent-server`"
                )
            try:
                module = __import__(
                    "langgraph.checkpoint.postgres.aio",
                    fromlist=["AsyncPostgresSaver"],
                )
            except ImportError as exc:
                raise RuntimeError(
                    'PostgreSQL checkpointing requires: python -m pip install -e ".[postgres]"'
                ) from exc
            context = module.AsyncPostgresSaver.from_conn_string(self._checkpoint_dsn)
            saver = await context.__aenter__()
            try:
                await saver.setup()
            except BaseException:
                await context.__aexit__(*__import__("sys").exc_info())
                raise
            self._checkpoint_context = context
            self._checkpointer = saver
            self.graph = self._build_graph()
            self._initialized = True

    async def close(self) -> None:
        if self._checkpoint_context is not None:
            await self._checkpoint_context.__aexit__(None, None, None)
            self._checkpoint_context = None
            self._initialized = False

    def _retrieval_lane_count(self, source_scope: SourceScope) -> int:
        scoped_count = getattr(self._retriever, "lane_count_for", None)
        if callable(scoped_count):
            return max(1, int(scoped_count(source_scope)))
        return max(1, int(self._retriever.lane_count))

    def _retrieval_source_names(self, source_scope: SourceScope) -> tuple[str, ...]:
        scoped_names = getattr(self._retriever, "source_names_for", None)
        if callable(scoped_names):
            return tuple(scoped_names(source_scope))
        return ("configured_retriever",)

    async def _retrieve(
        self,
        task: SearchTask,
        limit: int,
        source_scope: SourceScope,
    ) -> RetrievalBatch:
        if isinstance(self._retriever, ResearchRetriever):
            return await self._retriever.search(task, limit, source_scope)
        # Preserve compatibility with custom retrievers used by integrations and tests.
        return await self._retriever.search(task, limit)

    async def run(
        self,
        request: ResearchRequest,
        *,
        run_id: str | None = None,
        progress: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ResearchResult:
        await self.initialize()
        current_run_id = run_id or uuid4().hex
        max_queries = max(3, request.max_iterations * 3)
        retrieval_lanes = self._retrieval_lane_count(request.source_scope)
        budget = ResearchBudget(
            max_queries=max_queries,
            max_papers=request.max_papers,
            max_iterations=request.max_iterations,
            max_tool_calls=max_queries * retrieval_lanes,
            max_workers=request.max_workers,
            max_total_tokens=request.max_total_tokens,
            max_cost_usd=request.max_cost_usd,
            max_elapsed_seconds=request.max_elapsed_seconds,
        )
        initial: ResearchState = {
            "run_id": current_run_id,
            "request": request.model_dump(mode="json"),
            "status": RunStatus.RUNNING,
            "budget": budget.model_dump(mode="json"),
            "pending_tasks": [],
            "raw_papers": [],
            "raw_passages": [],
            "papers": [],
            "passages": [],
            "evidence": [],
            "claims": [],
            "verifications": [],
            "coverage_score": 0.0,
            "iteration": 1,
            "trace": [],
            "warnings": [],
            "model_invocations": [],
            "started_epoch_seconds": time.time(),
            "worker_assignments": [],
            "worker_outputs": [],
            "worker_results": [],
        }
        config = _graph_config(current_run_id)
        final = await self._invoke_graph(initial, config=config, progress=progress)
        return _to_result(final)

    async def resume(
        self,
        request: ResearchRequest,
        *,
        run_id: str,
        progress: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ResearchResult:
        await self.initialize()
        config = _graph_config(run_id)
        checkpoint = await self.graph.aget_state(config)
        if not checkpoint.values:
            return await self.run(request, run_id=run_id, progress=progress)
        final = await self._invoke_graph(None, config=config, progress=progress)
        return _to_result(final)

    async def _invoke_graph(
        self,
        graph_input: ResearchState | None,
        *,
        config: dict[str, Any],
        progress: Callable[[str, dict[str, Any]], Awaitable[None]] | None,
    ) -> ResearchState:
        if progress is None:
            final = await self.graph.ainvoke(graph_input, config=config)
        else:
            final = None
            async for mode, payload in self.graph.astream(
                graph_input,
                config=config,
                stream_mode=["updates", "values"],
            ):
                if mode == "values":
                    final = payload
                elif mode == "updates":
                    for node, update in payload.items():
                        details = _progress_update_details(str(node), update)
                        await progress(str(node), details)
            if final is None:
                raise RuntimeError("research graph completed without a final state")
        return cast(ResearchState, final)

    async def ingest(self, document: ParsedDocument) -> None:
        await self._retriever.upsert(document)

    async def get_artifact(self, reference: ArtifactRef) -> Any:
        return await self._artifact_store.get(reference.artifact_id, run_id=reference.run_id)

    def _build_graph(self) -> Any:
        builder = StateGraph(ResearchState)
        builder.add_node("plan", self._plan)
        builder.add_node("search", self._search)
        builder.add_node("dispatch_workers", self._dispatch_workers)
        builder.add_node("research_worker", self._research_worker)
        builder.add_node("collect_workers", self._collect_workers)
        builder.add_node("normalize", self._normalize)
        builder.add_node("extract_evidence", self._extract_evidence)
        builder.add_node("assess_coverage", self._assess_coverage)
        builder.add_node("refine", self._refine)
        builder.add_node("synthesize", self._synthesize)
        builder.add_node("split_claims", self._split_claims)
        builder.add_node("verify", self._verify)
        builder.add_node("quality_gate", self._quality_gate)
        builder.add_edge(START, "plan")
        builder.add_conditional_edges(
            "plan",
            self._route_after_plan,
            {"single": "search", "multi": "dispatch_workers"},
        )
        builder.add_conditional_edges(
            "dispatch_workers",
            self._send_workers,
            {"collect_workers": "collect_workers"},
        )
        builder.add_edge("research_worker", "collect_workers")
        builder.add_edge("collect_workers", "normalize")
        builder.add_edge("search", "normalize")
        builder.add_edge("normalize", "extract_evidence")
        builder.add_edge("extract_evidence", "assess_coverage")
        builder.add_conditional_edges(
            "assess_coverage",
            self._route_after_coverage,
            {"refine": "refine", "synthesize": "synthesize"},
        )
        builder.add_edge("refine", "search")
        builder.add_edge("synthesize", "split_claims")
        builder.add_edge("split_claims", "verify")
        builder.add_edge("verify", "quality_gate")
        builder.add_edge("quality_gate", END)
        return builder.compile(checkpointer=self._checkpointer)

    def _route_after_plan(self, state: ResearchState) -> Literal["single", "multi"]:
        plan = ResearchPlan.model_validate(state["plan"])
        if plan.complexity is Complexity.DEEP and len(plan.search_tasks) > 1:
            return "multi"
        return "single"

    async def _dispatch_workers(self, state: ResearchState) -> dict[str, Any]:
        budget = _budget_with_elapsed(state)
        request = ResearchRequest.model_validate(state["request"])
        retrieval_lanes = self._retrieval_lane_count(request.source_scope)
        tasks = [SearchTask.model_validate(item) for item in state.get("pending_tasks", [])]
        tool_capacity = (
            budget.max_tool_calls - budget.used_tool_calls
        ) // retrieval_lanes
        capacity = min(
            len(tasks),
            budget.remaining_queries,
            budget.remaining_workers,
            tool_capacity,
        )
        if budget.exhausted_limits:
            capacity = 0
        remaining_seconds = max(
            0.001,
            budget.max_elapsed_seconds - budget.elapsed_ms / 1_000,
        )
        assignments = [
            ResearchWorkerAssignment(
                worker_id=f"worker-{index}-{uuid4().hex[:8]}",
                run_id=state["run_id"],
                task=task,
                max_papers=budget.max_papers,
                timeout_seconds=min(self._worker_timeout_seconds, remaining_seconds),
                source_scope=request.source_scope,
            )
            for index, task in enumerate(tasks[:capacity], start=1)
        ]
        warnings = list(state.get("warnings", []))
        if not assignments:
            warnings.append("Supervisor skipped worker dispatch because a run limit was exhausted.")
        return {
            "budget": budget.model_dump(mode="json"),
            "worker_assignments": [item.model_dump(mode="json") for item in assignments],
            "warnings": warnings,
            "trace": _trace(
                state,
                "dispatch_workers",
                f"Supervisor dispatched {len(assignments)} bounded research workers.",
                {
                    "worker_ids": [item.worker_id for item in assignments],
                    "artifact_handoff": True,
                    "max_workers": budget.max_workers,
                    "source_scope": request.source_scope.value,
                    "sources": self._retrieval_source_names(request.source_scope),
                },
            ),
        }

    def _send_workers(self, state: ResearchState) -> list[Send] | Literal["collect_workers"]:
        assignments = state.get("worker_assignments", [])
        if not assignments:
            return "collect_workers"
        return [
            Send(
                "research_worker",
                {"worker_assignment": item, "worker_outputs": []},
                timeout=float(item["timeout_seconds"]) + 1,
            )
            for item in assignments
        ]

    async def _research_worker(self, state: WorkerState) -> dict[str, Any]:
        assignment = ResearchWorkerAssignment.model_validate(state["worker_assignment"])
        started = time.monotonic()
        artifact_ref: ArtifactRef | None = None
        status = WorkerStatus.COMPLETED
        error_type: str | None = None
        try:
            async with asyncio.timeout(assignment.timeout_seconds):
                batch = await self._retrieve(
                    assignment.task,
                    assignment.max_papers,
                    assignment.source_scope,
                )
                artifact_ref = await self._artifact_store.put(
                    run_id=assignment.run_id,
                    kind=ArtifactKind.RETRIEVAL_BATCH,
                    value=batch,
                )
        except TimeoutError:
            status = WorkerStatus.TIMED_OUT
            error_type = "TimeoutError"
        except Exception as exc:  # worker failures are isolated at the supervisor boundary
            status = WorkerStatus.FAILED
            error_type = type(exc).__name__
        result = ResearchWorkerResult(
            worker_id=assignment.worker_id,
            task_id=assignment.task.task_id,
            sub_question=assignment.task.sub_question,
            status=status,
            artifact_ref=artifact_ref,
            elapsed_ms=int((time.monotonic() - started) * 1_000),
            error_type=error_type,
        )
        return {"worker_outputs": [result.model_dump(mode="json")]}

    async def _collect_workers(self, state: ResearchState) -> dict[str, Any]:
        request = ResearchRequest.model_validate(state["request"])
        retrieval_lanes = self._retrieval_lane_count(request.source_scope)
        results = [
            ResearchWorkerResult.model_validate(item) for item in state.get("worker_outputs", [])
        ]
        papers = [Paper.model_validate(item) for item in state.get("papers", [])]
        passages = [Passage.model_validate(item) for item in state.get("raw_passages", [])]
        warnings = list(state.get("warnings", []))
        successful_artifacts = 0
        for result in results:
            if result.status is not WorkerStatus.COMPLETED or result.artifact_ref is None:
                warnings.append(
                    f"{result.worker_id}: {result.status} ({result.error_type or 'unknown'})"
                )
                continue
            try:
                artifact = await self._artifact_store.get(
                    result.artifact_ref.artifact_id,
                    run_id=state["run_id"],
                )
            except ArtifactNotFoundError:
                warnings.append(f"{result.worker_id}: retrieval artifact was not found")
                continue
            if not isinstance(artifact, RetrievalBatch):
                warnings.append(f"{result.worker_id}: unexpected artifact type")
                continue
            successful_artifacts += 1
            papers.extend(artifact.papers)
            passages.extend(artifact.passages)
            warnings.extend(artifact.errors)
        budget = _budget_with_elapsed(state).consume(
            queries=len(results),
            tool_calls=len(results) * retrieval_lanes,
            workers=len(results),
        )
        return {
            "budget": budget.model_dump(mode="json"),
            "raw_papers": [paper.model_dump(mode="json") for paper in papers],
            "raw_passages": [passage.model_dump(mode="json") for passage in passages],
            "worker_results": [item.model_dump(mode="json") for item in results],
            "warnings": warnings,
            "trace": _trace(
                state,
                "collect_workers",
                f"Supervisor merged {successful_artifacts}/{len(results)} worker artifacts.",
                {
                    "artifact_ids": [
                        item.artifact_ref.artifact_id
                        for item in results
                        if item.artifact_ref is not None
                    ],
                    "payloads_in_graph_state": False,
                    "worker_count": len(results),
                    "successful_workers": successful_artifacts,
                },
            ),
        }

    async def _plan(self, state: ResearchState) -> dict[str, Any]:
        request = ResearchRequest.model_validate(state["request"])
        try:
            async with asyncio.timeout(request.max_elapsed_seconds):
                reasoned = await self._reasoner.plan(request)
        except TimeoutError:
            fallback = await self._baseline_reasoner.plan(request)
            reasoned = Reasoned(
                fallback.value,
                fallback.model_invocations,
                ("plan exceeded the run deadline; deterministic fallback used.",),
            )
        plan = reasoned.value
        return {
            "plan": plan.model_dump(mode="json"),
            "pending_tasks": [task.model_dump(mode="json") for task in plan.search_tasks],
            **_reasoned_updates(state, (reasoned,)),
            "trace": _trace(
                state,
                "plan",
                f"Created {len(plan.search_tasks)} search tasks.",
                {
                    "complexity": plan.complexity.value,
                    "search_task_count": len(plan.search_tasks),
                    "sub_question_count": len(plan.sub_questions),
                },
            ),
        }

    async def _search(self, state: ResearchState) -> dict[str, Any]:
        budget = _budget_with_elapsed(state)
        request = ResearchRequest.model_validate(state["request"])
        retrieval_lanes = self._retrieval_lane_count(request.source_scope)
        tasks = [SearchTask.model_validate(item) for item in state.get("pending_tasks", [])]
        tool_capacity = (
            budget.max_tool_calls - budget.used_tool_calls
        ) // retrieval_lanes
        capacity = min(budget.remaining_queries, tool_capacity)
        selected = [] if budget.exhausted_limits else tasks[:capacity]
        if not selected:
            return {
                "budget": budget.model_dump(mode="json"),
                "raw_papers": state.get("papers", []),
                "warnings": [
                    *state.get("warnings", []),
                    f"Search budget exhausted: {budget.exhausted_limits or ('queries/tools',)}.",
                ],
                "trace": _trace(state, "search", "Skipped search because the budget is exhausted."),
            }
        batches = await asyncio.gather(
            *(
                self._retrieve(task, budget.max_papers, request.source_scope)
                for task in selected
            )
        )
        papers = [Paper.model_validate(item) for item in state.get("papers", [])]
        passages = [Passage.model_validate(item) for item in state.get("raw_passages", [])]
        errors: list[str] = []
        for batch in batches:
            papers.extend(batch.papers)
            passages.extend(batch.passages)
            errors.extend(batch.errors)
        consumed = budget.consume(
            queries=len(selected),
            tool_calls=len(selected) * retrieval_lanes,
        )
        warnings = [*state.get("warnings", []), *errors]
        return {
            "budget": consumed.model_dump(mode="json"),
            "raw_papers": [paper.model_dump(mode="json") for paper in papers],
            "raw_passages": [passage.model_dump(mode="json") for passage in passages],
            "warnings": warnings,
            "trace": _trace(
                state,
                "search",
                f"Executed {len(selected)} queries and collected {len(papers)} papers "
                f"and {len(passages)} passages.",
                {
                    "retrieval_errors": errors,
                    "retrieval_lanes": retrieval_lanes,
                    "source_scope": request.source_scope.value,
                    "sources": self._retrieval_source_names(request.source_scope),
                },
            ),
        }

    async def _normalize(self, state: ResearchState) -> dict[str, Any]:
        budget = ResearchBudget.model_validate(state["budget"])
        papers = [Paper.model_validate(item) for item in state.get("raw_papers", [])]
        unique = deduplicate_papers(papers)[: budget.max_papers]
        paper_ids = {paper.paper_id for paper in unique}
        selected_passages: dict[str, Passage] = {}
        for item in state.get("raw_passages", []):
            passage = Passage.model_validate(item)
            if passage.paper_id in paper_ids:
                key = passage.content_hash or passage.passage_id
                selected_passages.setdefault(key, passage)
        return {
            "papers": [paper.model_dump(mode="json") for paper in unique],
            "raw_passages": [
                passage.model_dump(mode="json") for passage in selected_passages.values()
            ],
            "trace": _trace(
                state,
                "normalize",
                f"Retained {len(unique)} unique papers and "
                f"{len(selected_passages)} full-text passages.",
                {
                    "paper_count": len(unique),
                    "passage_count": len(selected_passages),
                },
            ),
        }

    async def _extract_evidence(self, state: ResearchState) -> dict[str, Any]:
        request = ResearchRequest.model_validate(state["request"])
        question = request.question
        papers = tuple(Paper.model_validate(item) for item in state.get("papers", []))
        paper_by_id = {paper.paper_id: paper for paper in papers}
        retrieved = tuple(
            Passage.model_validate(item)
            for item in state.get("raw_passages", [])
            if item.get("paper_id") in paper_by_id
        )
        full_text_paper_ids = {passage.paper_id for passage in retrieved}
        abstract_fallback = tuple(
            Passage(
                paper_id=paper.paper_id,
                text=paper.abstract,
                end_char=len(paper.abstract),
            )
            for paper in papers
            if paper.abstract.strip() and paper.paper_id not in full_text_paper_ids
        )
        passages = (*retrieved, *abstract_fallback)
        reasoned_cards: list[Reasoned[EvidenceCard | None]] = []
        stage_budget = _budget_with_elapsed(state)
        concurrency = min(self._model_max_concurrency, request.max_workers)
        for offset in range(0, len(passages), concurrency):
            batch = passages[offset : offset + concurrency]
            use_model = (
                self._reasoner is not self._baseline_reasoner and not stage_budget.exhausted_limits
            )
            active_reasoner = self._reasoner if use_model else self._baseline_reasoner
            outputs = await asyncio.gather(
                *(
                    self._extract_with_deadline(
                        state,
                        question,
                        paper_by_id[passage.paper_id],
                        passage,
                        active_reasoner,
                        enforce_deadline=use_model,
                    )
                    for passage in batch
                )
            )
            reasoned_cards.extend(outputs)
            for output in outputs:
                stage_budget = _record_output_usage(stage_budget, output, state)
        evidence = tuple(item.value for item in reasoned_cards if item.value is not None)
        return {
            "passages": [passage.model_dump(mode="json") for passage in passages],
            "evidence": [card.model_dump(mode="json") for card in evidence],
            **_reasoned_updates(state, reasoned_cards),
            "trace": _trace(
                state,
                "extract_evidence",
                f"Created {len(evidence)} evidence cards.",
                {
                    "evidence_count": len(evidence),
                    "passage_count": len(passages),
                },
            ),
        }

    async def _assess_coverage(self, state: ResearchState) -> dict[str, Any]:
        evidence = [EvidenceCard.model_validate(item) for item in state.get("evidence", [])]
        unique_sources = len({card.paper_id for card in evidence})
        score = min(1.0, unique_sources / 3)
        return {
            "coverage_score": score,
            "trace": _trace(
                state,
                "assess_coverage",
                f"Coverage score={score:.2f} from {unique_sources} unique sources.",
                {
                    "coverage_score": score,
                    "unique_sources": unique_sources,
                    "decision": self._coverage_decision(state, score),
                },
            ),
        }

    def _route_after_coverage(self, state: ResearchState) -> Literal["refine", "synthesize"]:
        return self._coverage_decision(state, state.get("coverage_score", 0))

    def _coverage_decision(
        self,
        state: ResearchState,
        coverage_score: float,
    ) -> Literal["refine", "synthesize"]:
        budget = _budget_with_elapsed(state)
        if coverage_score >= 0.66:
            return "synthesize"
        if (
            state.get("iteration", 1) >= budget.max_iterations
            or budget.remaining_queries <= 0
            or bool(budget.exhausted_limits)
        ):
            return "synthesize"
        return "refine"

    async def _refine(self, state: ResearchState) -> dict[str, Any]:
        request = ResearchRequest.model_validate(state["request"])
        iteration = state.get("iteration", 1) + 1
        task = SearchTask(
            sub_question=f"Missing, limiting, or conflicting evidence for: {request.question}",
            query=f"{request.question} limitations conflicting evidence",
            purpose="close coverage gaps",
            iteration=iteration,
            year_from=request.year_from,
            year_to=request.year_to,
        )
        return {
            "iteration": iteration,
            "pending_tasks": [task.model_dump(mode="json")],
            "trace": _trace(
                state,
                "refine",
                f"Scheduled bounded refinement round {iteration}.",
                {"iteration": iteration, "purpose": task.purpose},
            ),
        }

    async def _synthesize(self, state: ResearchState) -> dict[str, Any]:
        question = ResearchRequest.model_validate(state["request"]).question
        papers = tuple(Paper.model_validate(item) for item in state.get("papers", []))
        evidence = tuple(EvidenceCard.model_validate(item) for item in state.get("evidence", []))
        use_model = not _budget_with_elapsed(state).exhausted_limits
        active_reasoner = self._reasoner if use_model else self._baseline_reasoner
        reasoned = await self._call_with_deadline(
            state,
            "synthesize",
            lambda: active_reasoner.synthesize(question, papers, evidence),
            lambda: self._baseline_reasoner.synthesize(question, papers, evidence),
            enforce_deadline=use_model,
        )
        report, claims = reasoned.value
        return {
            "report": report.model_dump(mode="json"),
            "claims": [claim.model_dump(mode="json") for claim in claims],
            **_reasoned_updates(state, (reasoned,)),
            "trace": _trace(
                state,
                "synthesize",
                f"Drafted a report with {len(claims)} claims.",
                {
                    "claim_count": len(claims),
                    "paper_count": len(papers),
                    "evidence_count": len(evidence),
                },
            ),
        }

    async def _split_claims(self, state: ResearchState) -> dict[str, Any]:
        claims = [AtomicClaim.model_validate(item) for item in state.get("claims", [])]
        non_atomic = [claim.claim_id for claim in claims if ";" in claim.text]
        warnings = list(state.get("warnings", []))
        if non_atomic:
            warnings.append(f"Potentially compound claims: {', '.join(non_atomic)}")
        return {
            "warnings": warnings,
            "trace": _trace(
                state,
                "split_claims",
                f"Validated {len(claims)} atomic claims.",
                {
                    "claim_count": len(claims),
                    "compound_claim_count": len(non_atomic),
                },
            ),
        }

    async def _verify(self, state: ResearchState) -> dict[str, Any]:
        request = ResearchRequest.model_validate(state["request"])
        claims = tuple(AtomicClaim.model_validate(item) for item in state.get("claims", []))
        evidence = tuple(EvidenceCard.model_validate(item) for item in state.get("evidence", []))
        passages = tuple(Passage.model_validate(item) for item in state.get("passages", []))
        reasoned_results: list[Reasoned[VerificationResult]] = []
        stage_budget = _budget_with_elapsed(state)
        concurrency = min(self._model_max_concurrency, request.max_workers)
        for offset in range(0, len(claims), concurrency):
            batch = claims[offset : offset + concurrency]
            use_model = (
                self._reasoner is not self._baseline_reasoner and not stage_budget.exhausted_limits
            )
            active_reasoner = self._reasoner if use_model else self._baseline_reasoner
            outputs = await asyncio.gather(
                *(
                    self._verify_with_deadline(
                        state,
                        claim,
                        evidence,
                        passages,
                        active_reasoner,
                        enforce_deadline=use_model,
                    )
                    for claim in batch
                )
            )
            reasoned_results.extend(outputs)
            for output in outputs:
                stage_budget = _record_output_usage(stage_budget, output, state)
        results = tuple(item.value for item in reasoned_results)
        verification_counts: dict[str, int] = {}
        for result in results:
            verification_counts[result.status.value] = (
                verification_counts.get(result.status.value, 0) + 1
            )
        return {
            "verifications": [result.model_dump(mode="json") for result in results],
            **_reasoned_updates(state, reasoned_results),
            "trace": _trace(
                state,
                "verify",
                f"Verified {len(results)} claims independently.",
                {
                    "claim_count": len(results),
                    "verification_counts": verification_counts,
                },
            ),
        }

    async def _extract_with_deadline(
        self,
        state: ResearchState,
        question: str,
        paper: Paper,
        passage: Passage,
        reasoner: ResearchReasoner,
        *,
        enforce_deadline: bool,
    ) -> Reasoned[EvidenceCard | None]:
        if not enforce_deadline:
            return await reasoner.extract_evidence(question, paper, passage)
        remaining_seconds = _remaining_seconds(state)
        if remaining_seconds > 0:
            try:
                async with asyncio.timeout(remaining_seconds):
                    return await reasoner.extract_evidence(question, paper, passage)
            except TimeoutError:
                pass
        secondary = await self._baseline_reasoner.extract_evidence(question, paper, passage)
        return Reasoned(
            secondary.value,
            secondary.model_invocations,
            (*secondary.warnings, "extract_evidence exceeded the run deadline; fallback used."),
        )

    async def _verify_with_deadline(
        self,
        state: ResearchState,
        claim: AtomicClaim,
        evidence: tuple[EvidenceCard, ...],
        passages: tuple[Passage, ...],
        reasoner: ResearchReasoner,
        *,
        enforce_deadline: bool,
    ) -> Reasoned[VerificationResult]:
        if not enforce_deadline:
            return await reasoner.verify(claim, evidence, passages)
        remaining_seconds = _remaining_seconds(state)
        if remaining_seconds > 0:
            try:
                async with asyncio.timeout(remaining_seconds):
                    return await reasoner.verify(claim, evidence, passages)
            except TimeoutError:
                pass
        secondary = await self._baseline_reasoner.verify(claim, evidence, passages)
        return Reasoned(
            secondary.value,
            secondary.model_invocations,
            (*secondary.warnings, "verify exceeded the run deadline; fallback used."),
        )

    async def _call_with_deadline(
        self,
        state: ResearchState,
        stage: str,
        primary: Callable[[], Awaitable[Reasoned[ReasonedValueT]]],
        fallback: Callable[[], Awaitable[Reasoned[ReasonedValueT]]],
        *,
        enforce_deadline: bool,
    ) -> Reasoned[ReasonedValueT]:
        if not enforce_deadline:
            return await primary()
        remaining_seconds = _remaining_seconds(state)
        if remaining_seconds <= 0:
            secondary = await fallback()
            return Reasoned(
                secondary.value,
                secondary.model_invocations,
                (*secondary.warnings, f"{stage} skipped after the run deadline."),
            )
        try:
            async with asyncio.timeout(remaining_seconds):
                return await primary()
        except TimeoutError:
            secondary = await fallback()
            return Reasoned(
                secondary.value,
                secondary.model_invocations,
                (*secondary.warnings, f"{stage} exceeded the run deadline; fallback used."),
            )

    async def _quality_gate(self, state: ResearchState) -> dict[str, Any]:
        results = [
            VerificationResult.model_validate(item) for item in state.get("verifications", [])
        ]
        blocked = any(
            result.status in {VerificationStatus.UNSUPPORTED, VerificationStatus.CONFLICT}
            for result in results
        )
        low_coverage = state.get("coverage_score", 0) < 0.66
        budget = _budget_with_elapsed(state)
        budget_limited = bool(budget.exhausted_limits)
        status = (
            RunStatus.NEEDS_REVIEW
            if blocked or low_coverage or not results or budget_limited
            else RunStatus.COMPLETED
        )
        warnings = list(state.get("warnings", []))
        if budget_limited:
            warnings.append(f"Run limits reached: {', '.join(budget.exhausted_limits)}")
        return {
            "status": status,
            "budget": budget.model_dump(mode="json"),
            "warnings": warnings,
            "trace": _trace(
                state,
                "quality_gate",
                f"Final status={status}; low_coverage={low_coverage}; "
                f"budget_limits={budget.exhausted_limits}.",
                {
                    "status": status.value,
                    "low_coverage": low_coverage,
                    "blocked_claims": blocked,
                    "budget_limits": budget.exhausted_limits,
                },
            ),
        }


def build_default_workflow(settings: Settings | None = None) -> ResearchWorkflow:
    current = settings or get_settings()
    providers: list[Any] = [OfflinePaperProvider()]
    if current.mcp_enabled:
        from research_agent.mcp_client import (
            build_mcp_paper_providers,
            parse_mcp_paper_servers,
        )

        mcp_configs = parse_mcp_paper_servers(current.mcp_paper_servers_json)
        if not mcp_configs:
            raise RuntimeError("MCP is enabled but RESEARCH_AGENT_MCP_PAPER_SERVERS_JSON is empty")
        providers.extend(
            build_mcp_paper_providers(
                mcp_configs,
                timeout_seconds=current.mcp_timeout_seconds,
            )
        )
    citation_graph: Any = None
    if current.provider_mode == "hybrid":
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(current.request_timeout_seconds),
            headers={"User-Agent": "research-agent-python-lab/1.1"},
            follow_redirects=False,
        )
        providers.extend(
            (
                OpenAlexPaperProvider(client=client, email=current.openalex_email),
                CrossrefPaperProvider(client=client, email=current.openalex_email),
            )
        )
        if current.semantic_scholar_enabled:
            from research_agent.semantic_scholar import SemanticScholarProvider

            citation_graph = SemanticScholarProvider(
                client=client,
                api_key=current.semantic_scholar_api_key,
            )
            providers.append(citation_graph)
    embedding_model = HashEmbeddingModel()
    if current.index_mode == "postgres":
        from research_agent.postgres_index import PostgresHybridIndex

        index: Any = PostgresHybridIndex(current.database_url, embedding_model)
    else:
        index = InMemoryHybridIndex(embedding_model)
    reranker: Any
    if current.reranker_mode == "cross_encoder":
        reranker = SentenceTransformerReranker(current.cross_encoder_model)
    else:
        reranker = DiversityReranker()
    artifact_store: ArtifactStore
    if current.artifact_store_mode == "s3":
        artifact_store = S3ArtifactStore(
            bucket=current.s3_bucket,
            endpoint_url=current.s3_endpoint_url,
            region=current.s3_region,
            access_key_id=current.s3_access_key_id,
            secret_access_key=current.s3_secret_access_key,
            prefix=current.s3_prefix,
        )
    else:
        artifact_store = InMemoryArtifactStore()
    reasoner: ResearchReasoner = DeterministicReasoner()
    if current.reasoner_mode in {"openai", "deepseek"}:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise RuntimeError(
                'OpenAI mode requires: python -m pip install -e ".[openai]"'
            ) from exc
        from research_agent.llm_reasoner import (
            FallbackResearchReasoner,
            LangChainStructuredReasoner,
        )

        structured_output_method: Literal["json_schema", "json_mode"]
        if current.reasoner_mode == "deepseek":
            if (
                current.deepseek_api_key is None
                or not current.deepseek_api_key.get_secret_value().strip()
            ):
                raise RuntimeError(
                    "DeepSeek mode requires DEEPSEEK_API_KEY or RESEARCH_AGENT_DEEPSEEK_API_KEY"
                )
            model_name = current.deepseek_model
            provider = "deepseek"
            structured_output_method = "json_mode"
            model = ChatOpenAI(
                model=model_name,
                api_key=current.deepseek_api_key,
                base_url=str(current.deepseek_base_url).rstrip("/"),
                timeout=current.model_timeout_seconds,
                max_retries=current.model_max_retries,
                use_responses_api=False,
            )
        else:
            model_name = current.model
            provider = "openai"
            structured_output_method = "json_schema"
            model = ChatOpenAI(
                model=model_name,
                timeout=current.model_timeout_seconds,
                max_retries=current.model_max_retries,
                use_responses_api=True,
                output_version="responses/v1",
            )
        reasoner = FallbackResearchReasoner(
            LangChainStructuredReasoner(
                model,
                provider=provider,
                model_name=model_name,
                structured_output_method=structured_output_method,
                input_cost_per_million_usd=current.model_input_cost_per_million_usd,
                output_cost_per_million_usd=current.model_output_cost_per_million_usd,
            )
        )
    return ResearchWorkflow(
        retriever=ResearchRetriever(
            metadata=CompositePaperProvider(tuple(providers)),
            index=index,
            citation_graph=citation_graph,
            reranker=reranker,
        ),
        reasoner=reasoner,
        artifact_store=artifact_store,
        worker_timeout_seconds=current.worker_timeout_seconds,
        model_max_concurrency=current.model_max_concurrency,
        checkpoint_dsn=(current.database_url if current.checkpoint_mode == "postgres" else None),
    )


def _graph_config(run_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": run_id},
        "recursion_limit": 40,
    }


def _trace(
    state: ResearchState,
    node: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    event = TraceEvent(node=node, message=message, details=details or {})
    return [*state.get("trace", []), event.model_dump(mode="json")]


def _progress_update_details(node: str, update: Any) -> dict[str, Any]:
    if not isinstance(update, dict):
        return {}
    details: dict[str, Any] = {"updated_fields": sorted(update)}
    trace_history = update.get("trace")
    if isinstance(trace_history, list) and trace_history:
        try:
            latest = TraceEvent.model_validate(trace_history[-1])
        except (TypeError, ValueError):
            latest = None
        if latest is not None and latest.node == node:
            details["trace_message"] = latest.message
            details["trace_details"] = latest.details
    if node == "research_worker":
        outputs = update.get("worker_outputs")
        if isinstance(outputs, list) and outputs:
            worker = ResearchWorkerResult.model_validate(outputs[-1])
            details["trace_message"] = (
                f"Worker {worker.worker_id} finished sub-question retrieval "
                f"with status={worker.status.value}."
            )
            details["trace_details"] = {
                "worker_status": worker.status.value,
                "elapsed_ms": worker.elapsed_ms,
                "sub_question": worker.sub_question,
            }
    return details


def _to_result(state: ResearchState) -> ResearchResult:
    return ResearchResult(
        run_id=state["run_id"],
        status=RunStatus(state["status"]),
        question=ResearchRequest.model_validate(state["request"]).question,
        plan=ResearchPlan.model_validate(state["plan"]),
        budget=ResearchBudget.model_validate(state["budget"]),
        papers=tuple(Paper.model_validate(item) for item in state.get("papers", [])),
        passages=tuple(Passage.model_validate(item) for item in state.get("passages", [])),
        evidence=tuple(EvidenceCard.model_validate(item) for item in state.get("evidence", [])),
        claims=tuple(AtomicClaim.model_validate(item) for item in state.get("claims", [])),
        verifications=tuple(
            VerificationResult.model_validate(item) for item in state.get("verifications", [])
        ),
        report=ResearchReport.model_validate(state["report"]),
        trace=tuple(TraceEvent.model_validate(item) for item in state.get("trace", [])),
        model_invocations=tuple(
            ModelInvocation.model_validate(item) for item in state.get("model_invocations", [])
        ),
        workers=tuple(
            ResearchWorkerResult.model_validate(item) for item in state.get("worker_results", [])
        ),
        warnings=tuple(state.get("warnings", [])),
    )


def _reasoned_updates(state: ResearchState, outputs: tuple[Any, ...] | list[Any]) -> dict[str, Any]:
    invocations = [
        ModelInvocation.model_validate(item) for item in state.get("model_invocations", [])
    ]
    warnings = list(state.get("warnings", []))
    for output in outputs:
        invocations.extend(output.model_invocations)
        warnings.extend(output.warnings)
    new_invocations = [item for output in outputs for item in output.model_invocations]
    tokens = sum((item.input_tokens or 0) + (item.output_tokens or 0) for item in new_invocations)
    cost = sum(item.estimated_cost_usd or 0 for item in new_invocations)
    current_budget = ResearchBudget.model_validate(state["budget"])
    budget = current_budget.record_usage(tokens=tokens, estimated_cost_usd=cost)
    newly_exhausted = set(budget.exhausted_limits) - set(current_budget.exhausted_limits)
    if newly_exhausted:
        warnings.append(
            f"Run limits reached after model invocation: {', '.join(sorted(newly_exhausted))}"
        )
    return {
        "model_invocations": [item.model_dump(mode="json") for item in invocations],
        "warnings": warnings,
        "budget": budget.model_dump(mode="json"),
    }


def _budget_with_elapsed(state: ResearchState) -> ResearchBudget:
    budget = ResearchBudget.model_validate(state["budget"])
    started = state.get("started_epoch_seconds")
    if started is None:
        return budget
    elapsed_ms = max(budget.elapsed_ms, int((time.time() - started) * 1_000))
    return budget.record_usage(elapsed_ms=elapsed_ms)


def _remaining_seconds(state: ResearchState) -> float:
    budget = _budget_with_elapsed(state)
    return max(0.0, budget.max_elapsed_seconds - budget.elapsed_ms / 1_000)


def _record_output_usage(
    budget: ResearchBudget,
    output: Any,
    state: ResearchState,
) -> ResearchBudget:
    tokens = sum(
        (item.input_tokens or 0) + (item.output_tokens or 0) for item in output.model_invocations
    )
    cost = sum(item.estimated_cost_usd or 0 for item in output.model_invocations)
    started = state.get("started_epoch_seconds")
    elapsed_ms = (
        max(budget.elapsed_ms, int((time.time() - started) * 1_000))
        if started is not None
        else budget.elapsed_ms
    )
    return budget.record_usage(
        tokens=tokens,
        estimated_cost_usd=cost,
        elapsed_ms=elapsed_ms,
    )
