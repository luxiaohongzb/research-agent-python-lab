from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any, Literal, TypeVar
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict

from research_agent.domain import (
    AtomicClaim,
    Complexity,
    EvidenceCard,
    EvidenceType,
    ModelInvocation,
    Paper,
    Passage,
    ReportSection,
    ResearchPlan,
    ResearchReport,
    ResearchRequest,
    SearchTask,
    VerificationResult,
    VerificationStatus,
)
from research_agent.model_stream import emit_model_stream
from research_agent.prompts import (
    EVIDENCE_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    PROMPT_VERSION,
    SYNTHESIS_SYSTEM_PROMPT,
    VERIFIER_SYSTEM_PROMPT,
)
from research_agent.reasoner import (
    DeterministicReasoner,
    Reasoned,
    ResearchReasoner,
    render_markdown,
    render_reference,
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)
ValueT = TypeVar("ValueT")


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryDraft(StrictSchema):
    sub_question: str
    query: str
    purpose: str


class PlanOutput(StrictSchema):
    complexity: Literal["SIMPLE", "DEEP"]
    objective: str
    sub_questions: list[str]
    queries: list[QueryDraft]
    inclusion_criteria: list[str]
    exclusion_criteria: list[str]


class EvidenceOutput(StrictSchema):
    relevant: bool
    atomic_finding: str
    evidence_type: Literal["DIRECT", "INDIRECT", "BACKGROUND"]
    limitations: list[str]
    source_quality: Literal["HIGH", "MEDIUM", "LOW"]
    confidence: float


class ClaimDraft(StrictSchema):
    text: str
    evidence_ids: list[str]


class SectionDraft(StrictSchema):
    heading: str
    claims: list[ClaimDraft]


class SynthesisOutput(StrictSchema):
    title: str
    executive_summary: str
    sections: list[SectionDraft]


class VerificationOutput(StrictSchema):
    status: Literal["SUPPORTED", "PARTIAL", "CONFLICT", "UNSUPPORTED"]
    confidence: float
    rationale: str


class StructuredReasoningError(RuntimeError):
    def __init__(self, stage: str, calls: tuple[ModelInvocation, ...]) -> None:
        super().__init__(f"structured model failed during {stage}")
        self.stage = stage
        self.calls = calls


class LangChainStructuredReasoner:
    """Provider-native structured output with semantic post-validation."""

    def __init__(
        self,
        model: Any,
        *,
        provider: str,
        model_name: str,
        structured_output_method: Literal["json_schema", "json_mode"] = "json_schema",
        schema_attempts: int = 2,
        stream_json_mode: bool = False,
        input_cost_per_million_usd: float | None = None,
        output_cost_per_million_usd: float | None = None,
    ) -> None:
        self._model = model
        self._provider = provider
        self._model_name = model_name
        self._structured_output_method = structured_output_method
        self._schema_attempts = schema_attempts
        self._stream_json_mode = stream_json_mode
        self._input_cost_per_million_usd = input_cost_per_million_usd
        self._output_cost_per_million_usd = output_cost_per_million_usd

    async def plan(self, request: ResearchRequest) -> Reasoned[ResearchPlan]:
        output = await self._invoke(
            "plan",
            PlanOutput,
            PLANNER_SYSTEM_PROMPT,
            request.model_dump(mode="json"),
        )
        draft = output.value
        if not 1 <= len(draft.queries) <= 6 or not draft.sub_questions:
            raise StructuredReasoningError("plan_semantic_validation", output.model_invocations)
        tasks = tuple(
            SearchTask(
                sub_question=item.sub_question.strip(),
                query=item.query.strip(),
                purpose=item.purpose.strip(),
                year_from=request.year_from,
                year_to=request.year_to,
            )
            for item in draft.queries
            if item.query.strip() and item.sub_question.strip()
        )
        if not tasks:
            raise StructuredReasoningError("plan_semantic_validation", output.model_invocations)
        plan = ResearchPlan(
            complexity=Complexity(draft.complexity),
            objective=draft.objective.strip() or request.question,
            sub_questions=tuple(item.strip() for item in draft.sub_questions if item.strip()),
            search_tasks=tasks,
            inclusion_criteria=tuple(draft.inclusion_criteria),
            exclusion_criteria=tuple(draft.exclusion_criteria),
        )
        return Reasoned(plan, output.model_invocations)

    async def extract_evidence(
        self, question: str, paper: Paper, passage: Passage
    ) -> Reasoned[EvidenceCard | None]:
        output = await self._invoke(
            "extract_evidence",
            EvidenceOutput,
            EVIDENCE_SYSTEM_PROMPT,
            {
                "question": question,
                "paper": {"paper_id": paper.paper_id, "title": paper.title, "year": paper.year},
                "passage": passage.model_dump(mode="json"),
            },
        )
        draft = output.value
        if not draft.relevant:
            return Reasoned(None, output.model_invocations)
        if not draft.atomic_finding.strip() or not 0 <= draft.confidence <= 1:
            raise StructuredReasoningError(
                "extract_evidence_semantic_validation", output.model_invocations
            )
        card = EvidenceCard(
            paper_id=paper.paper_id,
            passage_ids=(passage.passage_id,),
            atomic_finding=draft.atomic_finding.strip(),
            evidence_type=EvidenceType(draft.evidence_type),
            limitations=tuple(draft.limitations),
            source_quality=draft.source_quality,
            confidence=draft.confidence,
        )
        return Reasoned(card, output.model_invocations)

    async def synthesize(
        self,
        question: str,
        papers: tuple[Paper, ...],
        evidence: tuple[EvidenceCard, ...],
    ) -> Reasoned[tuple[ResearchReport, tuple[AtomicClaim, ...]]]:
        output = await self._invoke(
            "synthesize",
            SynthesisOutput,
            SYNTHESIS_SYSTEM_PROMPT,
            {
                "question": question,
                "evidence": [item.model_dump(mode="json") for item in evidence],
            },
        )
        draft = output.value
        evidence_by_id = {item.evidence_id: item for item in evidence}
        paper_by_id = {item.paper_id: item for item in papers}
        paper_order: list[str] = []
        for card in evidence:
            if card.paper_id not in paper_order and card.paper_id in paper_by_id:
                paper_order.append(card.paper_id)
        reference_number = {paper_id: index for index, paper_id in enumerate(paper_order, start=1)}
        claims: list[AtomicClaim] = []
        sections: list[ReportSection] = []
        for section_draft in draft.sections:
            section_claims: list[AtomicClaim] = []
            lines: list[str] = []
            for claim_draft in section_draft.claims:
                evidence_ids = tuple(dict.fromkeys(claim_draft.evidence_ids))
                if not evidence_ids or any(item not in evidence_by_id for item in evidence_ids):
                    raise StructuredReasoningError(
                        "synthesize_citation_validation", output.model_invocations
                    )
                claim = AtomicClaim(
                    text=claim_draft.text.strip(),
                    evidence_ids=evidence_ids,
                    section=section_draft.heading.strip(),
                )
                if not claim.text:
                    raise StructuredReasoningError(
                        "synthesize_claim_validation", output.model_invocations
                    )
                citation_numbers = sorted(
                    {
                        reference_number[evidence_by_id[item].paper_id]
                        for item in evidence_ids
                        if evidence_by_id[item].paper_id in reference_number
                    }
                )
                marker = "".join(f"[{number}]" for number in citation_numbers)
                lines.append(f"- {claim.text} {marker}".rstrip())
                claims.append(claim)
                section_claims.append(claim)
            sections.append(
                ReportSection(
                    heading=section_draft.heading.strip(),
                    body="\n".join(lines),
                    claim_ids=tuple(item.claim_id for item in section_claims),
                )
            )
        if evidence and not claims:
            raise StructuredReasoningError("synthesize_empty_claims", output.model_invocations)
        references = tuple(
            render_reference(reference_number[paper_id], paper_by_id[paper_id])
            for paper_id in paper_order
        )
        section_tuple = tuple(sections)
        report = ResearchReport(
            title=draft.title.strip() or f"Research brief: {question}",
            executive_summary=draft.executive_summary.strip(),
            sections=section_tuple,
            references=references,
            markdown=render_markdown(
                draft.title.strip() or f"Research brief: {question}",
                draft.executive_summary.strip(),
                section_tuple,
                references,
            ),
        )
        return Reasoned((report, tuple(claims)), output.model_invocations)

    async def verify(
        self,
        claim: AtomicClaim,
        evidence: tuple[EvidenceCard, ...],
        passages: tuple[Passage, ...],
    ) -> Reasoned[VerificationResult]:
        evidence_by_id = {item.evidence_id: item for item in evidence}
        passage_by_id = {item.passage_id: item for item in passages}
        checked = tuple(
            passage_id
            for evidence_id in claim.evidence_ids
            if (card := evidence_by_id.get(evidence_id))
            for passage_id in card.passage_ids
            if passage_id in passage_by_id
        )
        output = await self._invoke(
            "verify",
            VerificationOutput,
            VERIFIER_SYSTEM_PROMPT,
            {
                "claim": claim.model_dump(mode="json"),
                "passages": [passage_by_id[item].model_dump(mode="json") for item in checked],
            },
        )
        draft = output.value
        if not checked or not 0 <= draft.confidence <= 1:
            raise StructuredReasoningError("verify_semantic_validation", output.model_invocations)
        result = VerificationResult(
            claim_id=claim.claim_id,
            status=VerificationStatus(draft.status),
            confidence=draft.confidence,
            rationale=draft.rationale.strip(),
            checked_passage_ids=checked,
        )
        return Reasoned(result, output.model_invocations)

    async def _invoke(
        self,
        stage: str,
        schema: type[SchemaT],
        system_prompt: str,
        payload: dict[str, Any],
    ) -> Reasoned[SchemaT]:
        calls: list[ModelInvocation] = []
        last_error: BaseException | None = None
        for attempt in range(1, self._schema_attempts + 1):
            started = perf_counter()
            invocation_id = uuid4().hex[:12]
            try:
                structured_options: dict[str, Any] = {
                    "method": self._structured_output_method,
                    "include_raw": True,
                }
                if self._structured_output_method == "json_schema":
                    structured_options["strict"] = True
                runnable = self._model.with_structured_output(schema, **structured_options)
                effective_system_prompt = system_prompt
                if self._structured_output_method == "json_mode":
                    effective_system_prompt = (
                        f"{system_prompt}\nReturn exactly one JSON object that validates against "
                        f"this JSON Schema: {json.dumps(schema.model_json_schema())}"
                    )
                messages: list[SystemMessage | HumanMessage] = [
                    SystemMessage(content=effective_system_prompt),
                    HumanMessage(
                        content=json.dumps(
                            {"attempt": attempt, "input": payload},
                            ensure_ascii=False,
                        )
                    ),
                ]
                parsed: SchemaT
                raw: Any
                if self._stream_json_mode and self._structured_output_method == "json_mode":
                    parsed, raw = await self._stream_json_response(
                        stage=stage,
                        schema=schema,
                        messages=messages,
                        attempt=attempt,
                        invocation_id=invocation_id,
                    )
                else:
                    response = await runnable.ainvoke(messages)
                    candidate = response.get("parsed") if isinstance(response, dict) else None
                    parsing_error = (
                        response.get("parsing_error") if isinstance(response, dict) else None
                    )
                    raw = response.get("raw") if isinstance(response, dict) else None
                    if parsing_error or not isinstance(candidate, schema):
                        raise ValueError("model response failed schema validation")
                    parsed = candidate
                call = _model_call(
                    stage=stage,
                    provider=self._provider,
                    model_name=self._model_name,
                    raw=raw,
                    started=started,
                    success=True,
                    input_cost_per_million_usd=self._input_cost_per_million_usd,
                    output_cost_per_million_usd=self._output_cost_per_million_usd,
                )
                calls.append(call)
                return Reasoned(parsed, tuple(calls))
            except Exception as exc:
                last_error = exc
                calls.append(
                    _model_call(
                        stage=stage,
                        provider=self._provider,
                        model_name=self._model_name,
                        raw=None,
                        started=started,
                        success=False,
                        error_type=type(exc).__name__,
                        input_cost_per_million_usd=self._input_cost_per_million_usd,
                        output_cost_per_million_usd=self._output_cost_per_million_usd,
                    )
                )
        raise StructuredReasoningError(stage, tuple(calls)) from last_error

    async def _stream_json_response(
        self,
        *,
        stage: str,
        schema: type[SchemaT],
        messages: list[SystemMessage | HumanMessage],
        attempt: int,
        invocation_id: str,
    ) -> tuple[SchemaT, Any]:
        await emit_model_stream(
            stage,
            {
                "phase": "started",
                "invocation_id": invocation_id,
                "attempt": attempt,
                "provider": self._provider,
                "model": self._model_name,
                "stream_kind": "structured_output",
            },
        )
        raw: Any = None
        content = ""
        pending = ""
        try:
            async for chunk in self._model.astream(
                messages,
                response_format={"type": "json_object"},
            ):
                raw = chunk if raw is None else raw + chunk
                delta = _content_text(getattr(chunk, "content", ""))
                if not delta:
                    continue
                content += delta
                pending += delta
                if len(pending) >= 48 or pending.endswith(("\n", ".", "。", "!", "！")):
                    await emit_model_stream(
                        stage,
                        {
                            "phase": "delta",
                            "invocation_id": invocation_id,
                            "attempt": attempt,
                            "provider": self._provider,
                            "model": self._model_name,
                            "delta": pending,
                            "preview": content[-1600:],
                            "accumulated_chars": len(content),
                            "stream_kind": "structured_output",
                        },
                    )
                    pending = ""
            if pending:
                await emit_model_stream(
                    stage,
                    {
                        "phase": "delta",
                        "invocation_id": invocation_id,
                        "attempt": attempt,
                        "provider": self._provider,
                        "model": self._model_name,
                        "delta": pending,
                        "preview": content[-1600:],
                        "accumulated_chars": len(content),
                        "stream_kind": "structured_output",
                    },
                )
            parsed = schema.model_validate_json(_strip_json_fence(content))
            await emit_model_stream(
                stage,
                {
                    "phase": "completed",
                    "invocation_id": invocation_id,
                    "attempt": attempt,
                    "provider": self._provider,
                    "model": self._model_name,
                    "preview": content[-1600:],
                    "accumulated_chars": len(content),
                    "validated": True,
                    "stream_kind": "structured_output",
                },
            )
            return parsed, raw
        except Exception as exc:
            await emit_model_stream(
                stage,
                {
                    "phase": "failed",
                    "invocation_id": invocation_id,
                    "attempt": attempt,
                    "provider": self._provider,
                    "model": self._model_name,
                    "preview": content[-1600:],
                    "accumulated_chars": len(content),
                    "validated": False,
                    "error_type": type(exc).__name__,
                    "stream_kind": "structured_output",
                },
            )
            raise


class FallbackResearchReasoner:
    """Fall back per stage without losing failed model-call telemetry."""

    def __init__(
        self,
        primary: ResearchReasoner,
        fallback: ResearchReasoner | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback or DeterministicReasoner()

    async def plan(self, request: ResearchRequest) -> Reasoned[ResearchPlan]:
        return await self._call(
            "plan", self._primary.plan(request), lambda: self._fallback.plan(request)
        )

    async def extract_evidence(
        self, question: str, paper: Paper, passage: Passage
    ) -> Reasoned[EvidenceCard | None]:
        return await self._call(
            "extract_evidence",
            self._primary.extract_evidence(question, paper, passage),
            lambda: self._fallback.extract_evidence(question, paper, passage),
        )

    async def synthesize(
        self,
        question: str,
        papers: tuple[Paper, ...],
        evidence: tuple[EvidenceCard, ...],
    ) -> Reasoned[tuple[ResearchReport, tuple[AtomicClaim, ...]]]:
        return await self._call(
            "synthesize",
            self._primary.synthesize(question, papers, evidence),
            lambda: self._fallback.synthesize(question, papers, evidence),
        )

    async def verify(
        self,
        claim: AtomicClaim,
        evidence: tuple[EvidenceCard, ...],
        passages: tuple[Passage, ...],
    ) -> Reasoned[VerificationResult]:
        return await self._call(
            "verify",
            self._primary.verify(claim, evidence, passages),
            lambda: self._fallback.verify(claim, evidence, passages),
        )

    async def _call(
        self,
        stage: str,
        primary: Awaitable[Reasoned[ValueT]],
        fallback: Callable[[], Awaitable[Reasoned[ValueT]]],
    ) -> Reasoned[ValueT]:
        try:
            return await primary
        except StructuredReasoningError as exc:
            secondary = await fallback()
            return Reasoned(
                secondary.value,
                (*exc.calls, *secondary.model_invocations),
                (
                    *secondary.warnings,
                    f"{stage}: structured model failed; deterministic fallback used.",
                ),
            )


def _model_call(
    *,
    stage: str,
    provider: str,
    model_name: str,
    raw: Any,
    started: float,
    success: bool,
    error_type: str | None = None,
    input_cost_per_million_usd: float | None = None,
    output_cost_per_million_usd: float | None = None,
) -> ModelInvocation:
    usage = getattr(raw, "usage_metadata", None) or {}
    metadata = getattr(raw, "response_metadata", None) or {}
    observed_model = metadata.get("model_name") or metadata.get("model") or model_name
    input_tokens = _optional_int(usage.get("input_tokens"))
    output_tokens = _optional_int(usage.get("output_tokens"))
    estimated_cost = _estimated_cost(
        input_tokens,
        output_tokens,
        input_cost_per_million_usd,
        output_cost_per_million_usd,
    )
    return ModelInvocation(
        stage=stage,
        provider=provider,
        model=str(observed_model),
        prompt_version=PROMPT_VERSION,
        latency_ms=max(0, round((perf_counter() - started) * 1_000)),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost,
        success=success,
        error_type=error_type,
    )


def _content_text(content: Any) -> str:
    """Read only assistant output content; provider reasoning metadata is intentionally ignored."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            value = item.get("text") or item.get("content")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def _strip_json_fence(content: str) -> str:
    value = content.strip()
    if value.startswith("```json"):
        value = value[7:]
    elif value.startswith("```"):
        value = value[3:]
    if value.endswith("```"):
        value = value[:-3]
    return value.strip()


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and value >= 0 else None


def _estimated_cost(
    input_tokens: int | None,
    output_tokens: int | None,
    input_rate: float | None,
    output_rate: float | None,
) -> float | None:
    if input_tokens is None or output_tokens is None or input_rate is None or output_rate is None:
        return None
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
