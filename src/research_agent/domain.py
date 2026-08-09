from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class Complexity(StrEnum):
    SIMPLE = "SIMPLE"
    DEEP = "DEEP"


class EvidenceType(StrEnum):
    DIRECT = "DIRECT"
    INDIRECT = "INDIRECT"
    BACKGROUND = "BACKGROUND"


class VerificationStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    CONFLICT = "CONFLICT"
    UNSUPPORTED = "UNSUPPORTED"


class ResearchRequest(FrozenModel):
    question: str = Field(min_length=5, max_length=2_000)
    max_papers: int = Field(default=8, ge=1, le=50)
    max_iterations: int = Field(default=2, ge=1, le=5)
    year_from: int | None = Field(default=None, ge=1900, le=2100)
    year_to: int | None = Field(default=None, ge=1900, le=2100)

    @model_validator(mode="after")
    def validate_year_range(self) -> ResearchRequest:
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from must not be greater than year_to")
        return self


class ResearchBudget(FrozenModel):
    max_queries: int = Field(default=6, ge=1)
    max_papers: int = Field(default=8, ge=1)
    max_iterations: int = Field(default=2, ge=1)
    max_tool_calls: int = Field(default=12, ge=1)
    used_queries: int = Field(default=0, ge=0)
    used_tool_calls: int = Field(default=0, ge=0)

    @property
    def remaining_queries(self) -> int:
        return max(0, self.max_queries - self.used_queries)

    def consume(self, *, queries: int = 0, tool_calls: int = 0) -> ResearchBudget:
        if self.used_queries + queries > self.max_queries:
            raise BudgetExceededError("query budget exhausted")
        if self.used_tool_calls + tool_calls > self.max_tool_calls:
            raise BudgetExceededError("tool-call budget exhausted")
        return self.model_copy(
            update={
                "used_queries": self.used_queries + queries,
                "used_tool_calls": self.used_tool_calls + tool_calls,
            }
        )


class BudgetExceededError(RuntimeError):
    pass


class SearchTask(FrozenModel):
    task_id: str = Field(default_factory=lambda: f"search-{uuid4().hex[:12]}")
    sub_question: str
    query: str
    purpose: str
    iteration: int = Field(default=1, ge=1)
    year_from: int | None = None
    year_to: int | None = None


class ResearchPlan(FrozenModel):
    complexity: Complexity
    objective: str
    sub_questions: tuple[str, ...]
    search_tasks: tuple[SearchTask, ...]
    inclusion_criteria: tuple[str, ...]
    exclusion_criteria: tuple[str, ...]


class Paper(FrozenModel):
    paper_id: str
    title: str
    abstract: str = ""
    authors: tuple[str, ...] = ()
    year: int | None = None
    doi: str | None = None
    url: HttpUrl | None = None
    source: str
    external_ids: dict[str, str] = Field(default_factory=dict)
    score: float = Field(default=0.0, ge=0)


class Passage(FrozenModel):
    passage_id: str = Field(default_factory=lambda: f"passage-{uuid4().hex[:12]}")
    paper_id: str
    text: str
    section: str = "Abstract"
    page: int | None = None
    start_char: int = Field(default=0, ge=0)
    end_char: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_offsets(self) -> Passage:
        if self.end_char < self.start_char:
            raise ValueError("end_char must not be before start_char")
        return self


class EvidenceCard(FrozenModel):
    evidence_id: str = Field(default_factory=lambda: f"evidence-{uuid4().hex[:12]}")
    paper_id: str
    passage_ids: tuple[str, ...]
    atomic_finding: str
    evidence_type: EvidenceType
    limitations: tuple[str, ...] = ()
    source_quality: str = "MEDIUM"
    confidence: float = Field(ge=0, le=1)


class AtomicClaim(FrozenModel):
    claim_id: str = Field(default_factory=lambda: f"claim-{uuid4().hex[:12]}")
    text: str
    evidence_ids: tuple[str, ...]
    section: str


class VerificationResult(FrozenModel):
    claim_id: str
    status: VerificationStatus
    confidence: float = Field(ge=0, le=1)
    rationale: str
    checked_passage_ids: tuple[str, ...]


class ReportSection(FrozenModel):
    heading: str
    body: str
    claim_ids: tuple[str, ...]


class ResearchReport(FrozenModel):
    title: str
    executive_summary: str
    sections: tuple[ReportSection, ...]
    references: tuple[str, ...]
    markdown: str


class TraceEvent(FrozenModel):
    node: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResearchResult(FrozenModel):
    run_id: str
    status: RunStatus
    question: str
    plan: ResearchPlan
    budget: ResearchBudget
    papers: tuple[Paper, ...]
    passages: tuple[Passage, ...]
    evidence: tuple[EvidenceCard, ...]
    claims: tuple[AtomicClaim, ...]
    verifications: tuple[VerificationResult, ...]
    report: ResearchReport
    trace: tuple[TraceEvent, ...]
    warnings: tuple[str, ...] = ()


class RunSnapshot(FrozenModel):
    run_id: str
    status: RunStatus
    request: ResearchRequest
    result: ResearchResult | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
