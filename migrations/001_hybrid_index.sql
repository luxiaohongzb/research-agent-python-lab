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
