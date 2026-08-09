import pytest
from pydantic import ValidationError

from research_agent.domain import BudgetExceededError, ResearchBudget, ResearchRequest


def test_budget_is_immutable_and_enforced() -> None:
    budget = ResearchBudget(max_queries=2, max_papers=3, max_iterations=2, max_tool_calls=4)

    consumed = budget.consume(queries=1, tool_calls=2, workers=1)

    assert budget.used_queries == 0
    assert consumed.used_queries == 1
    assert consumed.used_workers == 1
    with pytest.raises(BudgetExceededError):
        consumed.consume(queries=2)


def test_budget_records_model_and_wall_clock_usage() -> None:
    budget = ResearchBudget().record_usage(
        tokens=1_500,
        estimated_cost_usd=0.25,
        elapsed_ms=2_000,
    )

    assert budget.used_total_tokens == 1_500
    assert budget.estimated_cost_usd == 0.25
    assert budget.elapsed_ms == 2_000

    exhausted = ResearchBudget(
        max_total_tokens=1_000,
        max_cost_usd=0.25,
        max_elapsed_seconds=2,
    ).record_usage(tokens=1_000, estimated_cost_usd=0.25, elapsed_ms=2_000)
    assert exhausted.exhausted_limits == ("tokens", "cost", "time")


def test_request_rejects_invalid_year_range() -> None:
    with pytest.raises(ValidationError):
        ResearchRequest(question="valid research question", year_from=2026, year_to=2020)
