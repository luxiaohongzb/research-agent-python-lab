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
    Paper,
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


class ResearchState(TypedDict, total=False):
    run_id: str
    request: dict[str, Any]
    status: str
    plan: dict[str, Any]
    budget: dict[str, Any]
    pending_tasks: list[dict[str, Any]]
    raw_papers: list[dict[str, Any]]
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


class ResearchWorkflow:
    def __init__(
        self,
        *,
        provider: CompositePaperProvider,
        reasoner: ResearchReasoner | None = None,
    ) -> None:
        self._provider = provider
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
            max_tool_calls=max_queries * self._provider.provider_count,
        )
        initial: ResearchState = {
            "run_id": current_run_id,
            "request": request.model_dump(mode="json"),
            "status": RunStatus.RUNNING,
            "budget": budget.model_dump(mode="json"),
            "pending_tasks": [],
            "raw_papers": [],
            "papers": [],
            "passages": [],
            "evidence": [],
            "claims": [],
            "verifications": [],
            "coverage_score": 0.0,
            "iteration": 1,
            "trace": [],
            "warnings": [],
        }
        final = await self.graph.ainvoke(
            initial,
            config={
                "configurable": {"thread_id": current_run_id},
                "recursion_limit": 40,
            },
        )
        return _to_result(final)

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
        plan = await self._reasoner.plan(request)
        return {
            "plan": plan.model_dump(mode="json"),
            "pending_tasks": [task.model_dump(mode="json") for task in plan.search_tasks],
            "trace": _trace(state, "plan", f"Created {len(plan.search_tasks)} search tasks."),
        }

    async def _search(self, state: ResearchState) -> dict[str, Any]:
        budget = ResearchBudget.model_validate(state["budget"])
        tasks = [SearchTask.model_validate(item) for item in state.get("pending_tasks", [])]
        tool_capacity = (
            budget.max_tool_calls - budget.used_tool_calls
        ) // self._provider.provider_count
        capacity = min(budget.remaining_queries, tool_capacity)
        selected = tasks[:capacity]
        if not selected:
            return {
                "raw_papers": state.get("papers", []),
                "warnings": [*state.get("warnings", []), "Search budget exhausted."],
                "trace": _trace(state, "search", "Skipped search because the budget is exhausted."),
            }
        batches = await asyncio.gather(
            *(self._provider.search(task, budget.max_papers) for task in selected)
        )
        papers = [Paper.model_validate(item) for item in state.get("papers", [])]
        errors: list[str] = []
        for batch in batches:
            papers.extend(batch.papers)
            errors.extend(batch.errors)
        consumed = budget.consume(
            queries=len(selected),
            tool_calls=len(selected) * self._provider.provider_count,
        )
        warnings = [*state.get("warnings", []), *errors]
        return {
            "budget": consumed.model_dump(mode="json"),
            "raw_papers": [paper.model_dump(mode="json") for paper in papers],
            "warnings": warnings,
            "trace": _trace(
                state,
                "search",
                f"Executed {len(selected)} queries and collected {len(papers)} candidates.",
                {"provider_errors": errors},
            ),
        }

    async def _normalize(self, state: ResearchState) -> dict[str, Any]:
        budget = ResearchBudget.model_validate(state["budget"])
        papers = [Paper.model_validate(item) for item in state.get("raw_papers", [])]
        unique = deduplicate_papers(papers)[: budget.max_papers]
        return {
            "papers": [paper.model_dump(mode="json") for paper in unique],
            "trace": _trace(state, "normalize", f"Retained {len(unique)} unique papers."),
        }

    async def _extract_evidence(self, state: ResearchState) -> dict[str, Any]:
        question = ResearchRequest.model_validate(state["request"]).question
        papers = tuple(Paper.model_validate(item) for item in state.get("papers", []))
        passages = tuple(
            Passage(
                paper_id=paper.paper_id,
                text=paper.abstract,
                end_char=len(paper.abstract),
            )
            for paper in papers
            if paper.abstract.strip()
        )
        paper_by_id = {paper.paper_id: paper for paper in papers}
        cards = await asyncio.gather(
            *(
                self._reasoner.extract_evidence(question, paper_by_id[passage.paper_id], passage)
                for passage in passages
            )
        )
        evidence = tuple(card for card in cards if card is not None)
        return {
            "passages": [passage.model_dump(mode="json") for passage in passages],
            "evidence": [card.model_dump(mode="json") for card in evidence],
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
        report, claims = await self._reasoner.synthesize(question, papers, evidence)
        return {
            "report": report.model_dump(mode="json"),
            "claims": [claim.model_dump(mode="json") for claim in claims],
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
        results = await asyncio.gather(
            *(self._reasoner.verify(claim, evidence, passages) for claim in claims)
        )
        return {
            "verifications": [result.model_dump(mode="json") for result in results],
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
    return ResearchWorkflow(provider=CompositePaperProvider(tuple(providers)))


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
        warnings=tuple(state.get("warnings", [])),
    )
