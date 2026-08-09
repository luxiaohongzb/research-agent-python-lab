ALTER TABLE research_runs ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default';
ALTER TABLE research_runs DROP CONSTRAINT IF EXISTS research_runs_idempotency_key_key;
CREATE UNIQUE INDEX IF NOT EXISTS research_runs_tenant_idempotency_idx
    ON research_runs(tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS audit_events (
    event_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    run_id text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_events_tenant_created_idx
    ON audit_events(tenant_id, created_at DESC);
