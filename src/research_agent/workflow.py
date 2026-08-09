from __future__ import annotations

import asyncio
import operator
import time
from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from research_agent.artifacts import ArtifactNotFoundError, InMemoryArtifactStore
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
from research_agent.reasoner import DeterministicReasoner, ResearchReasoner
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
    started_monotonic: float
    worker_assignments: list[dict[str, Any]]
    worker_outputs: Annotated[list[dict[str, Any]], operator.add]
    worker_results: list[dict[str, Any]]


class WorkerState(TypedDict):
    worker_assignment: dict[str, Any]
    worker_outputs: Annotated[list[dict[str, Any]], operator.add]


class ResearchWorkflow:
    def __init__(
        self,
        *,
        provider: CompositePaperProvider | None = None,
        retriever: ResearchRetriever | None = None,
        reasoner: ResearchReasoner | None = None,
        artifact_store: InMemoryArtifactStore | None = None,
        worker_timeout_seconds: float = 45.0,
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
        self._checkpointer = InMemorySaver()
        self.graph = self._build_graph()

    async def run(self, request: ResearchRequest, *, run_id: str | None = None) -> ResearchResult:
        current_run_id = run_id or uuid4().hex
        max_queries = max(3, request.max_iterations * 3)
        budget = ResearchBudget(
            max_queries=max_queries,
            max_papers=request.max_papers,
            max_iterations=request.max_iterations,
            max_tool_calls=max_queries * self._retriever.lane_count,
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
            "started_monotonic": time.monotonic(),
            "worker_assignments": [],
            "worker_outputs": [],
            "worker_results": [],
        }
        final = await self.graph.ainvoke(
            initial,
            config={
                "configurable": {"thread_id": current_run_id},
                "recursion_limit": 40,
            },
        )
        return _to_result(final)

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
        tasks = [SearchTask.model_validate(item) for item in state.get("pending_tasks", [])]
        tool_capacity = (
            budget.max_tool_calls - budget.used_tool_calls
        ) // self._retriever.lane_count
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
                batch = await self._retriever.search(assignment.task, assignment.max_papers)
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
            tool_calls=len(results) * self._retriever.lane_count,
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
                },
            ),
        }

    async def _plan(self, state: ResearchState) -> dict[str, Any]:
        request = ResearchRequest.model_validate(state["request"])
        reasoned = await self._reasoner.plan(request)
        plan = reasoned.value
        return {
            "plan": plan.model_dump(mode="json"),
            "pending_tasks": [task.model_dump(mode="json") for task in plan.search_tasks],
            **_reasoned_updates(state, (reasoned,)),
            "trace": _trace(state, "plan", f"Created {len(plan.search_tasks)} search tasks."),
        }

    async def _search(self, state: ResearchState) -> dict[str, Any]:
        budget = _budget_with_elapsed(state)
        tasks = [SearchTask.model_validate(item) for item in state.get("pending_tasks", [])]
        tool_capacity = (
            budget.max_tool_calls - budget.used_tool_calls
        ) // self._retriever.lane_count
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
            *(self._retriever.search(task, budget.max_papers) for task in selected)
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
            tool_calls=len(selected) * self._retriever.lane_count,
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
                {"retrieval_errors": errors, "retrieval_lanes": self._retriever.lane_count},
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
            ),
        }

    async def _extract_evidence(self, state: ResearchState) -> dict[str, Any]:
        question = ResearchRequest.model_validate(state["request"]).question
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
        if self._reasoner is self._baseline_reasoner:
            reasoned_cards = list(
                await asyncio.gather(
                    *(
                        self._reasoner.extract_evidence(
                            question, paper_by_id[passage.paper_id], passage
                        )
                        for passage in passages
                    )
                )
            )
        else:
            reasoned_cards = []
            stage_budget = _budget_with_elapsed(state)
            for passage in passages:
                active_reasoner = (
                    self._baseline_reasoner if stage_budget.exhausted_limits else self._reasoner
                )
                output = await active_reasoner.extract_evidence(
                    question,
                    paper_by_id[passage.paper_id],
                    passage,
                )
                reasoned_cards.append(output)
                stage_budget = _record_output_usage(stage_budget, output, state)
        evidence = tuple(item.value for item in reasoned_cards if item.value is not None)
        return {
            "passages": [passage.model_dump(mode="json") for passage in passages],
            "evidence": [card.model_dump(mode="json") for card in evidence],
            **_reasoned_updates(state, reasoned_cards),
            "trace": _trace(state, "extract_evidence", f"Created {len(evidence)} evidence cards."),
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
            ),
        }

    def _route_after_coverage(self, state: ResearchState) -> Literal["refine", "synthesize"]:
        budget = _budget_with_elapsed(state)
        if state.get("coverage_score", 0) >= 0.66:
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
            "trace": _trace(state, "refine", f"Scheduled bounded refinement round {iteration}."),
        }

    async def _synthesize(self, state: ResearchState) -> dict[str, Any]:
        question = ResearchRequest.model_validate(state["request"]).question
        papers = tuple(Paper.model_validate(item) for item in state.get("papers", []))
        evidence = tuple(EvidenceCard.model_validate(item) for item in state.get("evidence", []))
        active_reasoner = (
            self._baseline_reasoner
            if _budget_with_elapsed(state).exhausted_limits
            else self._reasoner
        )
        reasoned = await active_reasoner.synthesize(question, papers, evidence)
        report, claims = reasoned.value
        return {
            "report": report.model_dump(mode="json"),
            "claims": [claim.model_dump(mode="json") for claim in claims],
            **_reasoned_updates(state, (reasoned,)),
            "trace": _trace(state, "synthesize", f"Drafted a report with {len(claims)} claims."),
        }

    async def _split_claims(self, state: ResearchState) -> dict[str, Any]:
        claims = [AtomicClaim.model_validate(item) for item in state.get("claims", [])]
        non_atomic = [claim.claim_id for claim in claims if ";" in claim.text]
        warnings = list(state.get("warnings", []))
        if non_atomic:
            warnings.append(f"Potentially compound claims: {', '.join(non_atomic)}")
        return {
            "warnings": warnings,
            "trace": _trace(state, "split_claims", f"Validated {len(claims)} atomic claims."),
        }

    async def _verify(self, state: ResearchState) -> dict[str, Any]:
        claims = tuple(AtomicClaim.model_validate(item) for item in state.get("claims", []))
        evidence = tuple(EvidenceCard.model_validate(item) for item in state.get("evidence", []))
        passages = tuple(Passage.model_validate(item) for item in state.get("passages", []))
        if self._reasoner is self._baseline_reasoner:
            reasoned_results = list(
                await asyncio.gather(
                    *(self._reasoner.verify(claim, evidence, passages) for claim in claims)
                )
            )
        else:
            reasoned_results = []
            stage_budget = _budget_with_elapsed(state)
            for claim in claims:
                active_reasoner = (
                    self._baseline_reasoner if stage_budget.exhausted_limits else self._reasoner
                )
                output = await active_reasoner.verify(claim, evidence, passages)
                reasoned_results.append(output)
                stage_budget = _record_output_usage(stage_budget, output, state)
        results = tuple(item.value for item in reasoned_results)
        return {
            "verifications": [result.model_dump(mode="json") for result in results],
            **_reasoned_updates(state, reasoned_results),
            "trace": _trace(state, "verify", f"Verified {len(results)} claims independently."),
        }

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
            ),
        }


def build_default_workflow(settings: Settings | None = None) -> ResearchWorkflow:
    current = settings or get_settings()
    providers: list[Any] = [OfflinePaperProvider()]
    citation_graph: Any = None
    if current.provider_mode == "hybrid":
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(current.request_timeout_seconds),
            headers={"User-Agent": "research-agent-python-lab/0.1"},
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
    reasoner: ResearchReasoner = DeterministicReasoner()
    if current.reasoner_mode == "openai":
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

        model = ChatOpenAI(
            model=current.model,
            timeout=current.model_timeout_seconds,
            max_retries=current.model_max_retries,
            use_responses_api=True,
            output_version="responses/v1",
        )
        reasoner = FallbackResearchReasoner(
            LangChainStructuredReasoner(
                model,
                provider="openai",
                model_name=current.model,
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
        worker_timeout_seconds=current.worker_timeout_seconds,
    )


def _trace(
    state: ResearchState,
    node: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    event = TraceEvent(node=node, message=message, details=details or {})
    return [*state.get("trace", []), event.model_dump(mode="json")]


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
    started = state.get("started_monotonic")
    if started is None:
        return budget
    elapsed_ms = max(budget.elapsed_ms, int((time.monotonic() - started) * 1_000))
    return budget.record_usage(elapsed_ms=elapsed_ms)


def _record_output_usage(
    budget: ResearchBudget,
    output: Any,
    state: ResearchState,
) -> ResearchBudget:
    tokens = sum(
        (item.input_tokens or 0) + (item.output_tokens or 0) for item in output.model_invocations
    )
    cost = sum(item.estimated_cost_usd or 0 for item in output.model_invocations)
    started = state.get("started_monotonic")
    elapsed_ms = (
        max(budget.elapsed_ms, int((time.monotonic() - started) * 1_000))
        if started is not None
        else budget.elapsed_ms
    )
    return budget.record_usage(
        tokens=tokens,
        estimated_cost_usd=cost,
        elapsed_ms=elapsed_ms,
    )
