from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from research_agent.domain import (
    Paper,
    ParsedDocument,
    Passage,
    RetrievalHit,
    RetrievalLane,
)
from research_agent.retrieval import EmbeddingModel, HashEmbeddingModel, reciprocal_rank_fusion


class PostgresHybridIndex:
    """PostgreSQL full-text + pgvector index with application-side RRF."""

    def __init__(
        self,
        dsn: str,
        embedding_model: EmbeddingModel | None = None,
        *,
        rrf_k: int = 60,
    ) -> None:
        self._dsn = dsn
        self._embedding_model = embedding_model or HashEmbeddingModel()
        self._rrf_k = rrf_k
        self._pool: Any = None

    async def upsert(self, document: ParsedDocument) -> None:
        pool = await self._ensure_pool()
        vectors = [await self._embedding_model.embed(item.text) for item in document.passages]
        async with _acquire(pool) as connection, connection.transaction():
            await connection.execute(
                _UPSERT_PAPER,
                document.paper.paper_id,
                document.paper.title,
                document.paper.abstract,
                list(document.paper.authors),
                document.paper.year,
                document.paper.doi,
                str(document.paper.url) if document.paper.url else None,
                document.paper.source,
                document.paper.external_ids,
                document.paper.score,
            )
            await connection.execute(
                "DELETE FROM passages WHERE paper_id = $1", document.paper.paper_id
            )
            for passage, vector in zip(document.passages, vectors, strict=True):
                await connection.execute(
                    _INSERT_PASSAGE,
                    passage.passage_id,
                    passage.paper_id,
                    passage.text,
                    passage.section,
                    passage.page,
                    passage.start_char,
                    passage.end_char,
                    list(passage.coordinates),
                    passage.parser_version,
                    passage.content_hash,
                    self._vector(vector),
                )
            await connection.execute(
                "DELETE FROM citation_edges WHERE source_paper_id = $1",
                document.paper.paper_id,
            )
            for edge in document.citation_edges:
                await connection.execute(
                    _INSERT_EDGE,
                    edge.source_paper_id,
                    edge.target_paper_id,
                    edge.relation.value,
                    edge.source,
                )

    async def search(self, query: str, limit: int) -> tuple[RetrievalHit, ...]:
        if limit <= 0:
            return ()
        pool = await self._ensure_pool()
        vector = self._vector(await self._embedding_model.embed(query))
        candidate_limit = max(limit * 4, 20)
        async with _acquire(pool) as connection:
            keyword_rows = await connection.fetch(_KEYWORD_SEARCH, query, candidate_limit)
            vector_rows = await connection.fetch(_VECTOR_SEARCH, vector, candidate_limit)
        keyword_ids = [str(row["passage_id"]) for row in keyword_rows]
        vector_ids = [str(row["passage_id"]) for row in vector_rows]
        fused = reciprocal_rank_fusion(
            {
                RetrievalLane.KEYWORD: keyword_ids,
                RetrievalLane.VECTOR: vector_ids,
            },
            k=self._rrf_k,
        )
        row_by_id = {str(row["passage_id"]): row for row in [*keyword_rows, *vector_rows]}
        keyword_scores = {
            str(row["passage_id"]): float(row["retrieval_score"]) for row in keyword_rows
        }
        vector_scores = {
            str(row["passage_id"]): float(row["retrieval_score"]) for row in vector_rows
        }
        hits: list[RetrievalHit] = []
        for final_rank, (passage_id, fused_score) in enumerate(fused[:limit], start=1):
            row = row_by_id[passage_id]
            lane_ranks: dict[RetrievalLane, int] = {}
            if passage_id in keyword_ids:
                lane_ranks[RetrievalLane.KEYWORD] = keyword_ids.index(passage_id) + 1
            if passage_id in vector_ids:
                lane_ranks[RetrievalLane.VECTOR] = vector_ids.index(passage_id) + 1
            hits.append(
                RetrievalHit(
                    paper=_row_paper(row),
                    passage=_row_passage(row),
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

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        if self._embedding_model.dimensions != 256:
            raise ValueError("the current migration requires a 256-dimensional embedding model")
        try:
            asyncpg = importlib.import_module("asyncpg")
            pgvector_asyncpg = importlib.import_module("pgvector.asyncpg")
        except ImportError as exc:
            raise RuntimeError(
                'PostgreSQL mode requires: python -m pip install -e ".[postgres]"'
            ) from exc
        bootstrap = await asyncpg.connect(self._dsn)
        try:
            await bootstrap.execute(_SCHEMA)
        finally:
            await bootstrap.close()

        async def initialize(connection: Any) -> None:
            await connection.set_type_codec(
                "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )
            await pgvector_asyncpg.register_vector(connection)

        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=8, init=initialize)
        return self._pool

    @staticmethod
    def _vector(values: tuple[float, ...]) -> Any:
        vector_type = importlib.import_module("pgvector").Vector
        return vector_type(list(values))


def _acquire(pool: Any) -> AbstractAsyncContextManager[Any]:
    return cast(AbstractAsyncContextManager[Any], pool.acquire())


def _row_paper(row: Mapping[str, Any]) -> Paper:
    return Paper(
        paper_id=str(row["paper_id"]),
        title=str(row["title"]),
        abstract=str(row["abstract"]),
        authors=tuple(row["authors"] or ()),
        year=row["year"],
        doi=row["doi"],
        url=row["url"],
        source=str(row["source"]),
        external_ids=dict(row["external_ids"] or {}),
        score=float(row["paper_score"]),
    )


def _row_passage(row: Mapping[str, Any]) -> Passage:
    return Passage(
        passage_id=str(row["passage_id"]),
        paper_id=str(row["paper_id"]),
        text=str(row["text"]),
        section=str(row["section"]),
        page=row["page"],
        start_char=int(row["start_char"]),
        end_char=int(row["end_char"]),
        coordinates=tuple(row["coordinates"] or ()),
        parser_version=row["parser_version"],
        content_hash=row["content_hash"],
    )


_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS papers (
    paper_id text PRIMARY KEY,
    title text NOT NULL,
    abstract text NOT NULL DEFAULT '',
    authors jsonb NOT NULL DEFAULT '[]'::jsonb,
    year integer,
    doi text,
    url text,
    source text NOT NULL,
    external_ids jsonb NOT NULL DEFAULT '{}'::jsonb,
    score double precision NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS passages (
    passage_id text PRIMARY KEY,
    paper_id text NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
    text text NOT NULL,
    section text NOT NULL,
    page integer,
    start_char integer NOT NULL,
    end_char integer NOT NULL,
    coordinates jsonb NOT NULL DEFAULT '[]'::jsonb,
    parser_version text,
    content_hash text,
    embedding vector(256) NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED
);
CREATE TABLE IF NOT EXISTS citation_edges (
    source_paper_id text NOT NULL,
    target_paper_id text NOT NULL,
    relation text NOT NULL,
    source text NOT NULL,
    PRIMARY KEY (source_paper_id, target_paper_id, relation)
);
CREATE INDEX IF NOT EXISTS passages_search_gin ON passages USING gin(search_vector);
CREATE INDEX IF NOT EXISTS passages_embedding_hnsw
    ON passages USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS citation_edges_source_idx ON citation_edges(source_paper_id);
CREATE INDEX IF NOT EXISTS citation_edges_target_idx ON citation_edges(target_paper_id);
"""

_UPSERT_PAPER = """
INSERT INTO papers (
    paper_id, title, abstract, authors, year, doi, url, source, external_ids, score
) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9::jsonb, $10)
ON CONFLICT (paper_id) DO UPDATE SET
    title = EXCLUDED.title,
    abstract = EXCLUDED.abstract,
    authors = EXCLUDED.authors,
    year = EXCLUDED.year,
    doi = EXCLUDED.doi,
    url = EXCLUDED.url,
    source = EXCLUDED.source,
    external_ids = EXCLUDED.external_ids,
    score = EXCLUDED.score
"""

_INSERT_PASSAGE = """
INSERT INTO passages (
    passage_id, paper_id, text, section, page, start_char, end_char,
    coordinates, parser_version, content_hash, embedding
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11)
"""

_INSERT_EDGE = """
INSERT INTO citation_edges (source_paper_id, target_paper_id, relation, source)
VALUES ($1, $2, $3, $4)
ON CONFLICT (source_paper_id, target_paper_id, relation)
DO UPDATE SET source = EXCLUDED.source
"""

_RESULT_COLUMNS = """
p.passage_id, p.paper_id, p.text, p.section, p.page, p.start_char, p.end_char,
p.coordinates, p.parser_version, p.content_hash,
d.title, d.abstract, d.authors, d.year, d.doi, d.url, d.source,
d.external_ids, d.score AS paper_score
"""

_KEYWORD_SEARCH = f"""
SELECT {_RESULT_COLUMNS}, ts_rank_cd(p.search_vector, query) AS retrieval_score
FROM passages p
JOIN papers d ON d.paper_id = p.paper_id,
websearch_to_tsquery('simple', $1) query
WHERE p.search_vector @@ query
ORDER BY retrieval_score DESC, p.passage_id
LIMIT $2
"""

_VECTOR_SEARCH = f"""
SELECT {_RESULT_COLUMNS}, 1 - (p.embedding <=> $1) AS retrieval_score
FROM passages p
JOIN papers d ON d.paper_id = p.paper_id
ORDER BY p.embedding <=> $1, p.passage_id
LIMIT $2
"""
