from __future__ import annotations

import argparse
import asyncio

from research_agent.domain import ResearchRequest
from research_agent.evaluation import evaluate_result
from research_agent.workflow import build_default_workflow


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an evidence-first research workflow.")
    parser.add_argument("question", help="Research question")
    parser.add_argument("--max-papers", type=int, default=8)
    parser.add_argument("--max-iterations", type=int, default=2)
    parser.add_argument("--json", action="store_true", help="Print the complete JSON result")
    args = parser.parse_args()
    result = asyncio.run(
        build_default_workflow().run(
            ResearchRequest(
                question=args.question,
                max_papers=args.max_papers,
                max_iterations=args.max_iterations,
            )
        )
    )
    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(result.report.markdown)
        print("\n--- Quality metrics ---")
        print(evaluate_result(result).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
