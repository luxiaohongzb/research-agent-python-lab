import pytest

from research_agent.domain import ResearchRequest
from research_agent.evaluation import evaluate_result
from research_agent.workflow import build_default_workflow


@pytest.mark.asyncio
async def test_evaluation_reports_perfect_baseline_citations() -> None:
    result = await build_default_workflow().run(
        ResearchRequest(question="agentic RAG evidence verification")
    )

    metrics = evaluate_result(result)

    assert metrics.citation_precision == 1
    assert metrics.citation_coverage == 1
    assert metrics.supported_claim_rate == 1
    assert 0 < metrics.budget_utilization <= 1
