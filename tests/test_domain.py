import pytest
from pydantic import ValidationError

from research_agent.domain import BudgetExceededError, ResearchBudget, ResearchRequest


def test_budget_is_immutable_and_enforced() -> None:
    budget = ResearchBudget(max_queries=2, max_papers=3, max_iterations=2, max_tool_calls=4)

    consumed = budget.consume(queries=1, tool_calls=2)

    assert budget.used_queries == 0
    assert consumed.used_queries == 1
    with pytest.raises(BudgetExceededError):
        consumed.consume(queries=2)


def test_request_rejects_invalid_year_range() -> None:
    with pytest.raises(ValidationError):
        ResearchRequest(question="valid research question", year_from=2026, year_to=2020)
