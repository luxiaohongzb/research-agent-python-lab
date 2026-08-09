from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from research_agent.domain import ResearchResult, RunStatus, VerificationStatus


class EvaluationMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    citation_precision: float = Field(ge=0, le=1)
    citation_coverage: float = Field(ge=0, le=1)
    supported_claim_rate: float = Field(ge=0, le=1)
    source_diversity: int = Field(ge=0)
    budget_utilization: float = Field(ge=0, le=1)


class GoldenCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    language: Literal["en", "zh"]
    question: str
    expected_status: RunStatus
    min_papers: int = Field(ge=0)
    min_claims: int = Field(ge=0)
    min_citation_precision: float = Field(ge=0, le=1)
    min_citation_coverage: float = Field(ge=0, le=1)
    min_supported_claim_rate: float = Field(ge=0, le=1)


class CaseEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    passed: bool
    failures: tuple[str, ...]
    actual_status: RunStatus
    metrics: EvaluationMetrics


class SuiteEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    cases: tuple[CaseEvaluation, ...]


def evaluate_result(result: ResearchResult) -> EvaluationMetrics:
    evidence_ids = {card.evidence_id for card in result.evidence}
    cited_ids = [item for claim in result.claims for item in claim.evidence_ids]
    valid_citations = sum(item in evidence_ids for item in cited_ids)
    citation_precision = valid_citations / len(cited_ids) if cited_ids else 0.0
    claims_with_citation = sum(bool(claim.evidence_ids) for claim in result.claims)
    citation_coverage = claims_with_citation / len(result.claims) if result.claims else 0.0
    supported = sum(
        verification.status is VerificationStatus.SUPPORTED for verification in result.verifications
    )
    supported_rate = supported / len(result.verifications) if result.verifications else 0.0
    return EvaluationMetrics(
        citation_precision=citation_precision,
        citation_coverage=citation_coverage,
        supported_claim_rate=supported_rate,
        source_diversity=len({paper.source for paper in result.papers}),
        budget_utilization=result.budget.used_queries / result.budget.max_queries,
    )


def evaluate_case(case: GoldenCase, result: ResearchResult) -> CaseEvaluation:
    metrics = evaluate_result(result)
    failures: list[str] = []
    if result.status is not case.expected_status:
        failures.append(f"status expected={case.expected_status} actual={result.status}")
    if len(result.papers) < case.min_papers:
        failures.append(f"papers expected>={case.min_papers} actual={len(result.papers)}")
    if len(result.claims) < case.min_claims:
        failures.append(f"claims expected>={case.min_claims} actual={len(result.claims)}")
    thresholds = (
        ("citation_precision", metrics.citation_precision, case.min_citation_precision),
        ("citation_coverage", metrics.citation_coverage, case.min_citation_coverage),
        ("supported_claim_rate", metrics.supported_claim_rate, case.min_supported_claim_rate),
    )
    for name, actual, expected in thresholds:
        if actual < expected:
            failures.append(f"{name} expected>={expected:.2f} actual={actual:.2f}")
    return CaseEvaluation(
        case_id=case.case_id,
        passed=not failures,
        failures=tuple(failures),
        actual_status=result.status,
        metrics=metrics,
    )


def build_suite_evaluation(cases: list[CaseEvaluation]) -> SuiteEvaluation:
    passed = sum(item.passed for item in cases)
    return SuiteEvaluation(
        total=len(cases),
        passed=passed,
        pass_rate=passed / len(cases) if cases else 0,
        cases=tuple(cases),
    )


def load_golden_cases(path: str | Path) -> tuple[GoldenCase, ...]:
    source = Path(path)
    cases: list[GoldenCase] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(GoldenCase.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid golden case at {source}:{line_number}") from exc
    if not cases:
        raise ValueError(f"golden dataset is empty: {source}")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError(f"golden dataset contains duplicate case_id values: {source}")
    return tuple(cases)
