from __future__ import annotations

import asyncio
from typing import Any, Literal, TypedDict
from uuid import uuid4

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from research_agent.config import Settings, get_settings
from research_agent.domain import (
    AtomicClaim,
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
    RunStatus,
    SearchTask,
    TraceEvent,
    VerificationResult,
    VerificationStatus,
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


class ResearchWorkflow:
    def __init__(
        self,
        *,
        provider: CompositePaperProvider | None = None,
        retriever: ResearchRetriever | None = None,
        reasoner: ResearchReasoner | None = None,
    ) -> None:
        if retriever is None:
            if provider is None:
                raise ValueError("provider or retriever is required")
            retriever = ResearchRetriever(metadata=provider)
        self._retriever = retriever
        self._reasoner = reasoner or DeterministicReasoner()
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

    def _build_graph(self) -> Any:
        builder = StateGraph(ResearchState)
        builder.add_node("plan", self._plan)
        builder.add_node("search", self._search)
        builder.add_node("normalize", self._normalize)
        builder.add_node("extract_evidence", self._extract_evidence)
        builder.add_node("assess_coverage", self._assess_coverage)
        builder.add_node("refine", self._refine)
        builder.add_node("synthesize", self._synthesize)
        builder.add_node("split_claims", self._split_claims)
        builder.add_node("verify", self._verify)
        builder.add_node("quality_gate", self._quality_gate)
        builder.add_edge(START, "plan")
        builder.add_edge("plan", "search")
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
        budget = ResearchBudget.model_validate(state["budget"])
        tasks = [SearchTask.model_validate(item) for item in state.get("pending_tasks", [])]
        tool_capacity = (
            budget.max_tool_calls - budget.used_tool_calls
        ) // self._retriever.lane_count
        capacity = min(budget.remaining_queries, tool_capacity)
        selected = tasks[:capacity]
        if not selected:
            return {
                "raw_papers": state.get("papers", []),
                "warnings": [*state.get("warnings", []), "Search budget exhausted."],
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
        reasoned_cards = await asyncio.gather(
            *(
                self._reasoner.extract_evidence(question, paper_by_id[passage.paper_id], passage)
                for passage in passages
            )
        )
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
        budget = ResearchBudget.model_validate(state["budget"])
        if state.get("coverage_score", 0) >= 0.66:
            return "synthesize"
        if state.get("iteration", 1) >= budget.max_iterations or budget.remaining_queries <= 0:
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
        reasoned = await self._reasoner.synthesize(question, papers, evidence)
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
        reasoned_results = await asyncio.gather(
            *(self._reasoner.verify(claim, evidence, passages) for claim in claims)
        )
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
        status = (
            RunStatus.NEEDS_REVIEW
            if blocked or low_coverage or not results
            else RunStatus.COMPLETED
        )
        return {
            "status": status,
            "trace": _trace(
                state,
                "quality_gate",
                f"Final status={status}; low_coverage={low_coverage}.",
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
    return {
        "model_invocations": [item.model_dump(mode="json") for item in invocations],
        "warnings": warnings,
    }
