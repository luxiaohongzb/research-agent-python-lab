from __future__ import annotations

import argparse
import asyncio
import json

from research_agent.config import Settings
from research_agent.domain import ResearchRequest
from research_agent.evaluation import (
    CaseEvaluation,
    build_suite_evaluation,
    evaluate_case,
    load_golden_cases,
)
from research_agent.workflow import build_default_workflow


async def run_suite(dataset: str) -> list[CaseEvaluation]:
    workflow = build_default_workflow(
        Settings(provider_mode="offline", reasoner_mode="deterministic")
    )
    results: list[CaseEvaluation] = []
    for case in load_golden_cases(dataset):
        research = await workflow.run(ResearchRequest(question=case.question))
        results.append(evaluate_case(case, research))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the research workflow on JSONL cases.")
    parser.add_argument("dataset", help="Path to the golden JSONL dataset")
    parser.add_argument("--min-pass-rate", type=float, default=1.0)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.min_pass_rate <= 1:
        parser.error("--min-pass-rate must be between 0 and 1")
    report = build_suite_evaluation(asyncio.run(run_suite(args.dataset)))
    if args.summary_only:
        print(
            json.dumps(
                {
                    "total": report.total,
                    "passed": report.passed,
                    "pass_rate": report.pass_rate,
                    "failed_case_ids": [case.case_id for case in report.cases if not case.passed],
                },
                indent=2,
            )
        )
    else:
        print(report.model_dump_json(indent=2))
    if report.pass_rate < args.min_pass_rate:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
