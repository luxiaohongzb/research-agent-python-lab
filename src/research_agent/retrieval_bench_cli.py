from __future__ import annotations

import argparse
import asyncio

from research_agent.retrieval_benchmark import (
    load_benchmark_corpus,
    load_retrieval_cases,
    run_retrieval_benchmark,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark keyword, vector, and hybrid RRF retrieval on labeled JSONL data."
    )
    parser.add_argument("corpus", help="JSONL corpus with paper_id, title, and text")
    parser.add_argument("cases", help="JSONL cases with query and relevant_paper_ids")
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--min-hit-rate", type=float, default=0.0)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    if args.k < 1:
        parser.error("--k must be at least 1")
    if not 0 <= args.min_hit_rate <= 1:
        parser.error("--min-hit-rate must be between 0 and 1")

    report = asyncio.run(
        run_retrieval_benchmark(
            load_benchmark_corpus(args.corpus),
            load_retrieval_cases(args.cases),
            k=args.k,
        )
    )
    output = (
        report.model_dump_json(include={"k", "corpus_size", "case_count", "summaries"}, indent=2)
        if args.summary_only
        else report.model_dump_json(indent=2)
    )
    print(output)
    hybrid = next(item for item in report.summaries if item.method == "hybrid_rrf")
    if hybrid.hit_rate < args.min_hit_rate:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
