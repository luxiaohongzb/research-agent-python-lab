CREATE TABLE IF NOT EXISTS identity_users (
    user_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    email text NOT NULL,
    display_name text NOT NULL,
    password_hash text NOT NULL,
    roles jsonb NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    token_version integer NOT NULL DEFAULT 0,
    failed_login_attempts integer NOT NULL DEFAULT 0,
    locked_until timestamptz,
    last_login_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (tenant_id, email)
);
CREATE INDEX IF NOT EXISTS identity_users_tenant_created_idx
    ON identity_users(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS identity_refresh_sessions (
    session_id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES identity_users(user_id) ON DELETE CASCADE,
    tenant_id text NOT NULL,
    secret_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    revoked_at timestamptz,
    replaced_by text
);
CREATE INDEX IF NOT EXISTS identity_sessions_user_idx
    ON identity_refresh_sessions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS identity_sessions_expiry_idx
    ON identity_refresh_sessions(expires_at) WHERE revoked_at IS NULL;
