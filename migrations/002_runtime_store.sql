CREATE TABLE IF NOT EXISTS research_runs (
    run_id text PRIMARY KEY,
    idempotency_key text UNIQUE,
    snapshot jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS research_runs_updated_at_idx
    ON research_runs(updated_at DESC);

-- LangGraph checkpoint tables are owned and migrated by
-- AsyncPostgresSaver.setup(), not duplicated in this application migration.
