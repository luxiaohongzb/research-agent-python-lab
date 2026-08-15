from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field

from research_agent.domain import Paper, ParsedDocument, Passage, RetrievalHit, RetrievalLane
from research_agent.retrieval import InMemoryHybridIndex


class BenchmarkCorpusDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    paper_id: str
    title: str
    text: str
    source: str = "benchmark"


class RetrievalBenchmarkCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    query: str
    relevant_paper_ids: tuple[str, ...] = Field(min_length=1)


class RetrievalMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    hit_rate: float = Field(ge=0, le=1)
    recall_at_k: float = Field(ge=0, le=1)
    reciprocal_rank: float = Field(ge=0, le=1)
    ndcg_at_k: float = Field(ge=0, le=1)


class RetrievalCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    method: str
    retrieved_paper_ids: tuple[str, ...]
    metrics: RetrievalMetrics


class RetrievalMethodSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    method: str
    cases: int = Field(ge=0)
    hit_rate: float = Field(ge=0, le=1)
    recall_at_k: float = Field(ge=0, le=1)
    mean_reciprocal_rank: float = Field(ge=0, le=1)
    ndcg_at_k: float = Field(ge=0, le=1)


class RetrievalBenchmarkReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    k: int = Field(ge=1)
    corpus_size: int = Field(ge=1)
    case_count: int = Field(ge=1)
    summaries: tuple[RetrievalMethodSummary, ...]
    results: tuple[RetrievalCaseResult, ...]


async def run_retrieval_benchmark(
    corpus: tuple[BenchmarkCorpusDocument, ...],
    cases: tuple[RetrievalBenchmarkCase, ...],
    *,
    k: int = 3,
) -> RetrievalBenchmarkReport:
    if not corpus:
        raise ValueError("benchmark corpus is empty")
    if not cases:
        raise ValueError("retrieval benchmark cases are empty")
    if k < 1:
        raise ValueError("k must be at least 1")
    corpus_ids = {document.paper_id for document in corpus}
    missing = {
        paper_id
        for case in cases
        for paper_id in case.relevant_paper_ids
        if paper_id not in corpus_ids
    }
    if missing:
        raise ValueError(
            f"benchmark relevance judgments reference unknown papers: {sorted(missing)}"
        )

    index = InMemoryHybridIndex()
    for document in corpus:
        await index.upsert(_parsed_document(document))

    results: list[RetrievalCaseResult] = []
    for case in cases:
        hits = await index.search(case.query, max(k * 4, len(corpus)))
        for method, ranking in _paper_rankings(hits).items():
            selected = tuple(ranking[:k])
            results.append(
                RetrievalCaseResult(
                    case_id=case.case_id,
                    method=method,
                    retrieved_paper_ids=selected,
                    metrics=evaluate_ranking(selected, case.relevant_paper_ids, k=k),
                )
            )

    return RetrievalBenchmarkReport(
        k=k,
        corpus_size=len(corpus),
        case_count=len(cases),
        summaries=_summaries(results),
        results=tuple(results),
    )


def evaluate_ranking(
    retrieved_ids: tuple[str, ...],
    relevant_ids: tuple[str, ...],
    *,
    k: int,
) -> RetrievalMetrics:
    relevant = set(relevant_ids)
    ranking = tuple(dict.fromkeys(retrieved_ids))[:k]
    relevant_ranks = [rank for rank, item in enumerate(ranking, start=1) if item in relevant]
    hits = len(relevant_ranks)
    ideal_hits = min(k, len(relevant))
    dcg = sum(1 / math.log2(rank + 1) for rank in relevant_ranks)
    ideal_dcg = sum(1 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return RetrievalMetrics(
        hit_rate=float(bool(hits)),
        recall_at_k=hits / len(relevant),
        reciprocal_rank=1 / relevant_ranks[0] if relevant_ranks else 0,
        ndcg_at_k=dcg / ideal_dcg if ideal_dcg else 0,
    )


def load_benchmark_corpus(path: str | Path) -> tuple[BenchmarkCorpusDocument, ...]:
    return _load_jsonl(path, BenchmarkCorpusDocument)


def load_retrieval_cases(path: str | Path) -> tuple[RetrievalBenchmarkCase, ...]:
    cases = _load_jsonl(path, RetrievalBenchmarkCase)
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError(f"retrieval benchmark contains duplicate case IDs: {path}")
    return cases


def _parsed_document(document: BenchmarkCorpusDocument) -> ParsedDocument:
    passage = Passage(
        passage_id=f"passage-{document.paper_id}",
        paper_id=document.paper_id,
        text=document.text,
        end_char=len(document.text),
    )
    return ParsedDocument(
        paper=Paper(
            paper_id=document.paper_id,
            title=document.title,
            abstract=document.text,
            source=document.source,
        ),
        passages=(passage,),
        document_hash=f"benchmark-{document.paper_id}",
        parser="benchmark",
        parser_version="1",
    )


def _paper_rankings(hits: tuple[RetrievalHit, ...]) -> dict[str, list[str]]:
    return {
        "keyword": _rank_by_lane(hits, RetrievalLane.KEYWORD),
        "vector": _rank_by_lane(hits, RetrievalLane.VECTOR),
        "hybrid_rrf": list(
            dict.fromkeys(
                hit.paper.paper_id for hit in sorted(hits, key=lambda item: item.final_rank)
            )
        ),
    }


def _rank_by_lane(hits: tuple[RetrievalHit, ...], lane: RetrievalLane) -> list[str]:
    ranked = sorted(
        (hit for hit in hits if lane in hit.lane_ranks),
        key=lambda hit: hit.lane_ranks[lane],
    )
    return list(dict.fromkeys(hit.paper.paper_id for hit in ranked))


def _summaries(results: list[RetrievalCaseResult]) -> tuple[RetrievalMethodSummary, ...]:
    methods = sorted({result.method for result in results})
    summaries: list[RetrievalMethodSummary] = []
    for method in methods:
        selected = [result.metrics for result in results if result.method == method]
        count = len(selected)
        summaries.append(
            RetrievalMethodSummary(
                method=method,
                cases=count,
                hit_rate=sum(item.hit_rate for item in selected) / count,
                recall_at_k=sum(item.recall_at_k for item in selected) / count,
                mean_reciprocal_rank=sum(item.reciprocal_rank for item in selected) / count,
                ndcg_at_k=sum(item.ndcg_at_k for item in selected) / count,
            )
        )
    return tuple(summaries)


ModelT = TypeVar("ModelT", bound=BaseModel)


def _load_jsonl(path: str | Path, model: type[ModelT]) -> tuple[ModelT, ...]:
    source = Path(path)
    values: list[ModelT] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            values.append(model.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid benchmark record at {source}:{line_number}") from exc
    if not values:
        raise ValueError(f"benchmark dataset is empty: {source}")
    return tuple(values)
