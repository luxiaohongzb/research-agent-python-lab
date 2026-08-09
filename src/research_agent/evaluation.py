from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from research_agent.domain import ResearchResult, VerificationStatus


class EvaluationMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    citation_precision: float = Field(ge=0, le=1)
    citation_coverage: float = Field(ge=0, le=1)
    supported_claim_rate: float = Field(ge=0, le=1)
    source_diversity: int = Field(ge=0)
    budget_utilization: float = Field(ge=0, le=1)


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
