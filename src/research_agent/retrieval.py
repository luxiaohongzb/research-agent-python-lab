from __future__ import annotations

import asyncio
import hashlib
import importlib
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

from research_agent.domain import (
    Paper,
    ParsedDocument,
    Passage,
    RetrievalHit,
    RetrievalLane,
    SearchTask,
    SourceScope,
)
from research_agent.providers import (
    CompositePaperProvider,
    deduplicate_papers,
    relevance,
    tokenize,
)


class EmbeddingModel(Protocol):
    dimensions: int
    model_name: str

    async def embed(self, text: str) -> tuple[float, ...]: ...


class HybridIndex(Protocol):
    async def upsert(self, document: ParsedDocument) -> None: ...

    async def search(self, query: str, limit: int) -> tuple[RetrievalHit, ...]: ...


class PassageReranker(Protocol):
    async def rerank(
        self, query: str, hits: tuple[RetrievalHit, ...], limit: int
    ) -> tuple[RetrievalHit, ...]: ...


class CitationGraphProvider(Protocol):
    name: str

    async def expand(self, seed: Paper, limit: int) -> list[Paper]: ...


@dataclass(frozen=True)
class RetrievalBatch:
    papers: tuple[Paper, ...]
    passages: tuple[Passage, ...]
    hits: tuple[RetrievalHit, ...]
    errors: tuple[str, ...]
    diagnostics: tuple[dict[str, Any], ...] = ()


class HashEmbeddingModel:
    """Deterministic local baseline; use a scientific embedding model in production."""

    model_name = "hash-embedding-v1"

    def __init__(self, dimensions: int = 256) -> None:
        if dimensions < 32:
            raise ValueError("embedding dimensions must be at least 32")
        self.dimensions = dimensions

    async def embed(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self.dimensions
        for token, frequency in Counter(tokenize(text)).items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign * (1 + math.log(frequency))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return tuple(vector)


class InMemoryHybridIndex:
    def __init__(self, embedding_model: EmbeddingModel | None = None, *, rrf_k: int = 60) -> None:
        self._embedding_model = embedding_model or HashEmbeddingModel()
        self._rrf_k = rrf_k
        self._papers: dict[str, Paper] = {}
        self._passages: dict[str, Passage] = {}
        self._vectors: dict[str, tuple[float, ...]] = {}

    async def upsert(self, document: ParsedDocument) -> None:
        self._papers[document.paper.paper_id] = document.paper
        for passage in document.passages:
            self._passages[passage.passage_id] = passage
            self._vectors[passage.passage_id] = await self._embedding_model.embed(passage.text)

    async def search(self, query: str, limit: int) -> tuple[RetrievalHit, ...]:
        if not self._passages or limit <= 0:
            return ()
        query_tokens = tokenize(query)
        query_vector = await self._embedding_model.embed(query)
        keyword_scores = self._bm25(query_tokens)
        vector_scores = {
            passage_id: _cosine(query_vector, vector)
            for passage_id, vector in self._vectors.items()
        }
        keyword_ranked = _rank_positive(keyword_scores, limit * 4)
        vector_ranked = _rank_positive(vector_scores, limit * 4, threshold=0.05)
        fused = reciprocal_rank_fusion(
            {
                RetrievalLane.KEYWORD: keyword_ranked,
                RetrievalLane.VECTOR: vector_ranked,
            },
            k=self._rrf_k,
        )
        hits: list[RetrievalHit] = []
        for final_rank, (passage_id, fused_score) in enumerate(fused[:limit], start=1):
            passage = self._passages[passage_id]
            paper = self._papers[passage.paper_id]
            lane_ranks = {
                lane: rank
                for lane, ranking in (
                    (RetrievalLane.KEYWORD, keyword_ranked),
                    (RetrievalLane.VECTOR, vector_ranked),
                )
                if (rank := _rank_of(passage_id, ranking)) is not None
            }
            hits.append(
                RetrievalHit(
                    paper=paper,
                    passage=passage,
                    lane_ranks=lane_ranks,
                    lane_scores={
                        RetrievalLane.KEYWORD: keyword_scores.get(passage_id, 0),
                        RetrievalLane.VECTOR: vector_scores.get(passage_id, 0),
                    },
                    fused_score=fused_score,
                    final_rank=final_rank,
                )
            )
        return tuple(hits)

    def _bm25(self, query_tokens: set[str]) -> dict[str, float]:
        documents = {
            passage_id: Counter(tokenize(passage.text))
            for passage_id, passage in self._passages.items()
        }
        count = len(documents)
        average_length = sum(sum(values.values()) for values in documents.values()) / max(1, count)
        document_frequency = {
            token: sum(token in values for values in documents.values()) for token in query_tokens
        }
        scores: dict[str, float] = {}
        for passage_id, frequencies in documents.items():
            length = sum(frequencies.values())
            score = 0.0
            for token in query_tokens:
                frequency = frequencies.get(token, 0)
                if not frequency:
                    continue
                inverse_frequency = math.log(
                    1
                    + (count - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5)
                )
                denominator = frequency + 1.2 * (0.25 + 0.75 * length / max(1, average_length))
                score += inverse_frequency * frequency * 2.2 / denominator
            scores[passage_id] = score
        return scores


class DiversityReranker:
    def __init__(self, *, max_per_paper: int = 2) -> None:
        self._max_per_paper = max_per_paper

    async def rerank(
        self, query: str, hits: tuple[RetrievalHit, ...], limit: int
    ) -> tuple[RetrievalHit, ...]:
        scored = sorted(
            hits,
            key=lambda hit: hit.fused_score + relevance(query, hit.passage.text) * 0.05,
            reverse=True,
        )
        selected: list[RetrievalHit] = []
        paper_counts: Counter[str] = Counter()
        for hit in scored:
            if paper_counts[hit.paper.paper_id] >= self._max_per_paper:
                continue
            paper_counts[hit.paper.paper_id] += 1
            selected.append(hit.model_copy(update={"final_rank": len(selected) + 1}))
            if len(selected) >= limit:
                break
        return tuple(selected)


class SentenceTransformerReranker:
    """Optional cross-encoder adapter loaded only when the ML extra is installed."""

    def __init__(self, model_name: str, *, max_per_paper: int = 2) -> None:
        try:
            sentence_transformers = importlib.import_module("sentence_transformers")
        except ImportError as exc:
            raise RuntimeError('Cross-encoder requires: python -m pip install -e ".[ml]"') from exc
        self._model: Any = sentence_transformers.CrossEncoder(model_name)
        self._max_per_paper = max_per_paper

    async def rerank(
        self, query: str, hits: tuple[RetrievalHit, ...], limit: int
    ) -> tuple[RetrievalHit, ...]:
        if not hits:
            return ()
        scores = await asyncio.to_thread(
            self._model.predict,
            [(query, hit.passage.text) for hit in hits],
        )
        ranked = sorted(
            zip(hits, scores, strict=True), key=lambda item: float(item[1]), reverse=True
        )
        selected: list[RetrievalHit] = []
        counts: Counter[str] = Counter()
        for hit, score in ranked:
            if counts[hit.paper.paper_id] >= self._max_per_paper:
                continue
            counts[hit.paper.paper_id] += 1
            lane_scores = {**hit.lane_scores, RetrievalLane.RERANK: float(score)}
            selected.append(
                hit.model_copy(update={"lane_scores": lane_scores, "final_rank": len(selected) + 1})
            )
            if len(selected) >= limit:
                break
        return tuple(selected)


class ResearchRetriever:
    def __init__(
        self,
        *,
        metadata: CompositePaperProvider,
        index: HybridIndex | None = None,
        citation_graph: CitationGraphProvider | None = None,
        reranker: PassageReranker | None = None,
    ) -> None:
        self._metadata = metadata
        self._index = index
        self._citation_graph = citation_graph
        self._reranker = reranker or DiversityReranker()

    @property
    def lane_count(self) -> int:
        return self.lane_count_for(SourceScope.AUTO)

    def lane_count_for(self, source_scope: SourceScope) -> int:
        metadata_count = self._metadata.provider_count_for(source_scope)
        include_index = source_scope in {SourceScope.AUTO, SourceScope.PRIVATE, SourceScope.ALL}
        include_graph = source_scope in {SourceScope.AUTO, SourceScope.PUBLIC, SourceScope.ALL}
        return max(
            1,
            metadata_count
            + int(include_index and self._index is not None)
            + int(include_graph and self._citation_graph is not None and metadata_count > 0),
        )

    def source_names_for(self, source_scope: SourceScope) -> tuple[str, ...]:
        names = list(self._metadata.provider_names_for(source_scope))
        if source_scope in {SourceScope.AUTO, SourceScope.PRIVATE, SourceScope.ALL} and self._index:
            names.append("uploaded_documents")
        if (
            source_scope in {SourceScope.AUTO, SourceScope.PUBLIC, SourceScope.ALL}
            and self._citation_graph
            and self._metadata.provider_count_for(source_scope) > 0
        ):
            names.append(self._citation_graph.name)
        return tuple(dict.fromkeys(names))

    async def search(
        self,
        task: SearchTask,
        limit: int,
        source_scope: SourceScope = SourceScope.AUTO,
    ) -> RetrievalBatch:
        include_index = source_scope in {SourceScope.AUTO, SourceScope.PRIVATE, SourceScope.ALL}
        include_graph = source_scope in {SourceScope.AUTO, SourceScope.PUBLIC, SourceScope.ALL}
        metadata_future = self._metadata.search(task, limit, source_scope)
        index_future = (
            self._index.search(task.query, limit * 4)
            if include_index and self._index
            else _empty_hits()
        )
        metadata_batch, index_hits = await asyncio.gather(metadata_future, index_future)
        errors = list(metadata_batch.errors)
        if not self.source_names_for(source_scope):
            errors.append(f"source_scope={source_scope.value}: no retrieval source is configured")
        graph_papers: list[Paper] = []
        if include_graph and self._citation_graph:
            graph_results = await asyncio.gather(
                *(
                    self._citation_graph.expand(seed, max(2, limit // 2))
                    for seed in metadata_batch.papers[:2]
                ),
                return_exceptions=True,
            )
            for result in graph_results:
                if isinstance(result, BaseException):
                    errors.append(f"{self._citation_graph.name}: {type(result).__name__}")
                else:
                    graph_papers.extend(result)
        paper_rankings = {
            RetrievalLane.METADATA: [paper.paper_id for paper in metadata_batch.papers],
            RetrievalLane.CITATION_GRAPH: [paper.paper_id for paper in graph_papers],
            RetrievalLane.KEYWORD: _paper_ranking(index_hits, RetrievalLane.KEYWORD),
            RetrievalLane.VECTOR: _paper_ranking(index_hits, RetrievalLane.VECTOR),
        }
        paper_scores = dict(reciprocal_rank_fusion(paper_rankings))
        all_papers = [*metadata_batch.papers, *graph_papers, *(hit.paper for hit in index_hits)]
        candidates = deduplicate_papers(all_papers)
        indexed_text = {
            paper_id: " ".join(
                hit.passage.text for hit in index_hits if hit.paper.paper_id == paper_id
            )
            for paper_id in {hit.paper.paper_id for hit in index_hits}
        }
        decisions = [
            _candidate_decision(
                task,
                paper,
                paper_scores.get(paper.paper_id, 0),
                supporting_text=indexed_text.get(paper.paper_id, ""),
            )
            for paper in candidates
        ]
        accepted_ids = {str(decision["paper_id"]) for decision in decisions if decision["accepted"]}
        papers = sorted(
            (
                paper.model_copy(
                    update={
                        # RRF is a rank-fusion signal, not a relevance probability.
                        # Persist the independently calculated query relevance instead.
                        "score": float(
                            next(
                                item["relevance_score"]
                                for item in decisions
                                if item["paper_id"] == paper.paper_id
                            )
                        )
                    }
                )
                for paper in candidates
                if paper.paper_id in accepted_ids
            ),
            key=lambda paper: (-paper.score, -(paper.year or 0), paper.paper_id),
        )[:limit]
        selected_ids = {paper.paper_id for paper in papers}
        decisions = [
            {
                **decision,
                "selected": decision["paper_id"] in selected_ids,
                "reason": (
                    "selected by relevance-gated Top-K"
                    if decision["paper_id"] in selected_ids
                    else decision["reason"]
                    if not decision["accepted"]
                    else "relevant candidate ranked below Top-K"
                ),
            }
            for decision in decisions
        ]
        reranked = await self._reranker.rerank(task.query, index_hits, limit * 2)
        reranked = tuple(
            hit
            for hit in reranked
            if hit.paper.paper_id in selected_ids
            and _passage_is_relevant(task, hit.paper, hit.passage)
        )
        return RetrievalBatch(
            papers=tuple(papers),
            passages=tuple(hit.passage for hit in reranked),
            hits=reranked,
            errors=tuple(errors),
            diagnostics=tuple(decisions),
        )

    async def upsert(self, document: ParsedDocument) -> None:
        if self._index is None:
            raise RuntimeError("document ingestion requires a configured hybrid index")
        await self._index.upsert(document)


def reciprocal_rank_fusion(
    rankings: dict[RetrievalLane, list[str]], *, k: int = 60
) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings.values():
        for rank, item_id in enumerate(dict.fromkeys(ranking), start=1):
            scores[item_id] = scores.get(item_id, 0) + 1 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def _rank_positive(scores: dict[str, float], limit: int, *, threshold: float = 0) -> list[str]:
    return [
        item_id
        for item_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        if score > threshold
    ][:limit]


def _rank_of(item_id: str, ranking: list[str]) -> int | None:
    try:
        return ranking.index(item_id) + 1
    except ValueError:
        return None


def _paper_ranking(hits: tuple[RetrievalHit, ...], lane: RetrievalLane) -> list[str]:
    ordered = sorted(
        (hit for hit in hits if lane in hit.lane_ranks),
        key=lambda hit: hit.lane_ranks[lane],
    )
    return list(dict.fromkeys(hit.paper.paper_id for hit in ordered))


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


async def _empty_hits() -> tuple[RetrievalHit, ...]:
    return ()


def _candidate_decision(
    task: SearchTask,
    paper: Paper,
    rrf_score: float,
    *,
    supporting_text: str = "",
) -> dict[str, Any]:
    query = f"{task.query} {task.sub_question}"
    query_tokens = tokenize(query)
    title_tokens = tokenize(paper.title)
    body_tokens = tokenize(f"{paper.title} {paper.abstract} {supporting_text}")
    overlap = query_tokens & body_tokens
    title_overlap = query_tokens & title_tokens
    required_overlap = min(2, max(1, len(query_tokens) // 4))
    accepted = len(overlap) >= required_overlap
    score = 0.7 * (len(overlap) / max(1, len(query_tokens))) + 0.3 * (
        len(title_overlap) / max(1, min(len(query_tokens), 5))
    )
    if not paper.abstract.strip() and not supporting_text.strip() and not title_overlap:
        accepted = False
        reason = "rejected: no query terms in title and no searchable abstract"
    elif not accepted:
        reason = f"rejected: only {len(overlap)}/{required_overlap} required query terms matched"
    else:
        reason = "passed relevance gate"
    return {
        "paper_id": paper.paper_id,
        "title": paper.title,
        "source": paper.source,
        "accepted": accepted,
        "selected": False,
        "relevance_score": round(score, 4),
        "rrf_score": round(rrf_score, 6),
        "matched_terms": sorted(overlap)[:8],
        "reason": reason,
    }


def _passage_is_relevant(task: SearchTask, paper: Paper, passage: Passage) -> bool:
    query_tokens = tokenize(f"{task.query} {task.sub_question}")
    text_tokens = tokenize(f"{paper.title} {passage.text}")
    required_overlap = min(2, max(1, len(query_tokens) // 4))
    return len(query_tokens & text_tokens) >= required_overlap
