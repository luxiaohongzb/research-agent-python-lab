import pytest

from research_agent.domain import ResearchRequest, RunStatus, VerificationStatus
from research_agent.workflow import build_default_workflow


@pytest.mark.asyncio
async def test_workflow_produces_traceable_supported_claims() -> None:
    workflow = build_default_workflow()

    result = await workflow.run(
        ResearchRequest(question="How do agentic RAG and claim verification work together?")
    )

    assert result.status is RunStatus.COMPLETED
    assert len(result.papers) >= 2
    assert len(result.claims) == len(result.verifications)
    assert all(item.evidence_ids for item in result.claims)
    assert all(item.status is VerificationStatus.SUPPORTED for item in result.verifications)
    assert result.workers == ()
    assert result.budget.used_workers == 0
    assert [event.node for event in result.trace] == [
        "plan",
        "search",
        "normalize",
        "extract_evidence",
        "assess_coverage",
        "synthesize",
        "split_claims",
        "verify",
        "quality_gate",
    ]
    coverage = next(event for event in result.trace if event.node == "assess_coverage")
    assert coverage.details["coverage_score"] >= 0.66
    assert coverage.details["decision"] == "synthesize"
    quality_gate = result.trace[-1]
    assert quality_gate.details["status"] == RunStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_no_evidence_stops_after_bounded_refinement() -> None:
    workflow = build_default_workflow()

    result = await workflow.run(ResearchRequest(question="zzzyyyxxx qqqvvvbbb", max_iterations=2))

    assert result.status is RunStatus.NEEDS_REVIEW
    assert result.claims == ()
    assert result.budget.used_queries == 2
    assert sum(event.node == "refine" for event in result.trace) == 1
    assert "Evidence is insufficient" in result.report.executive_summary
