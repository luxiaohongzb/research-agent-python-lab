from pathlib import Path

import pytest

from research_agent.eval_cli import run_suite
from research_agent.evaluation import build_suite_evaluation, load_golden_cases

DATASET = Path(__file__).parents[1] / "datasets" / "golden.jsonl"


def test_golden_dataset_has_unique_bilingual_cases() -> None:
    cases = load_golden_cases(DATASET)

    assert len(cases) == 24
    assert {case.language for case in cases} == {"en", "zh"}
    assert len({case.case_id for case in cases}) == len(cases)


@pytest.mark.asyncio
async def test_offline_baseline_passes_all_golden_quality_gates() -> None:
    report = build_suite_evaluation(await run_suite(str(DATASET)))

    assert report.total == 24
    assert report.pass_rate == 1
