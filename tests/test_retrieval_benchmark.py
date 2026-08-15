from __future__ import annotations

from pathlib import Path

import pytest

from research_agent.retrieval_benchmark import (
    evaluate_ranking,
    load_benchmark_corpus,
    load_retrieval_cases,
    run_retrieval_benchmark,
)

ROOT = Path(__file__).parents[1]


def test_evaluate_ranking_reports_recall_mrr_and_ndcg() -> None:
    metrics = evaluate_ranking(("irrelevant", "relevant"), ("relevant",), k=2)

    assert metrics.hit_rate == 1
    assert metrics.recall_at_k == 1
    assert metrics.reciprocal_rank == 0.5
    assert 0 < metrics.ndcg_at_k < 1


@pytest.mark.asyncio
async def test_bundled_retrieval_benchmark_compares_all_lanes() -> None:
    report = await run_retrieval_benchmark(
        load_benchmark_corpus(ROOT / "datasets" / "retrieval_corpus.jsonl"),
        load_retrieval_cases(ROOT / "datasets" / "retrieval_cases.jsonl"),
        k=2,
    )

    assert report.corpus_size == 4
    assert report.case_count == 8
    assert {summary.method for summary in report.summaries} == {
        "keyword",
        "vector",
        "hybrid_rrf",
    }
    hybrid = next(summary for summary in report.summaries if summary.method == "hybrid_rrf")
    assert hybrid.hit_rate == 1
    assert hybrid.recall_at_k == 1
