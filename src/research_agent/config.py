from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from RESEARCH_AGENT_* variables."""

    model_config = SettingsConfigDict(
        env_prefix="RESEARCH_AGENT_",
        env_file=".env",
        extra="ignore",
    )

    provider_mode: Literal["offline", "hybrid"] = "offline"
    reasoner_mode: Literal["deterministic", "openai", "deepseek"] = "deterministic"
    index_mode: Literal["memory", "postgres"] = "memory"
    checkpoint_mode: Literal["memory", "postgres"] = "memory"
    run_store_mode: Literal["memory", "postgres"] = "memory"
    event_broker_mode: Literal["memory", "redis"] = "memory"
    dispatch_mode: Literal["local", "redis"] = "local"
    cancellation_mode: Literal["memory", "redis"] = "memory"
    artifact_store_mode: Literal["memory", "s3"] = "memory"
    auth_mode: Literal["disabled", "api_key", "rbac", "hybrid"] = "disabled"
    api_keys_json: str = "{}"
    identity_store_mode: Literal["auto", "memory", "postgres"] = "auto"
    auth_token_secret: SecretStr | None = None
    auth_issuer: str = "research-agent"
    auth_audience: str = "research-agent-api"
    auth_access_token_seconds: int = Field(default=900, ge=60, le=86_400)
    auth_refresh_token_seconds: int = Field(default=2_592_000, ge=300, le=31_536_000)
    auth_max_failed_attempts: int = Field(default=5, ge=1, le=100)
    auth_lockout_seconds: int = Field(default=900, ge=10, le=86_400)
    bootstrap_admin_tenant_id: str | None = None
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None
    bootstrap_admin_display_name: str = "System Administrator"
    tenant_max_active_runs: int = Field(default=5, ge=1, le=1_000)
    tenant_max_workers: int = Field(default=5, ge=1, le=5)
    tenant_max_total_tokens: int = Field(default=1_000_000, ge=1_000, le=10_000_000)
    tenant_max_cost_usd: float = Field(default=100.0, ge=0.01, le=1_000)
    reranker_mode: Literal["lexical", "cross_encoder"] = "lexical"
    openalex_email: str | None = None
    semantic_scholar_enabled: bool = False
    semantic_scholar_api_key: str | None = None
    mcp_enabled: bool = False
    mcp_paper_servers_json: str = "[]"
    mcp_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    database_url: str = "postgresql://research:research@127.0.0.1:5432/research_agent"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_prefix: str = "research-agent"
    redis_consumer_group: str = "research-workers"
    queue_lease_seconds: int = Field(default=60, ge=5, le=3_600)
    worker_metrics_port: int = Field(default=9_100, ge=1_024, le=65_535)
    s3_endpoint_url: str | None = None
    s3_bucket: str = "research-artifacts"
    s3_region: str = "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_prefix: str = "artifacts"
    grobid_url: str = "http://127.0.0.1:8070"
    grobid_timeout_seconds: float = Field(default=120.0, gt=0, le=300)
    cross_encoder_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    worker_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    otel_enabled: bool = False
    otel_service_name: str = "research-agent-python-lab"
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    default_max_papers: int = Field(default=8, ge=1, le=50)
    default_max_iterations: int = Field(default=2, ge=1, le=5)
    model: str = "gpt-5-mini"
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_base_url: HttpUrl = HttpUrl("https://api.deepseek.com")
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DEEPSEEK_API_KEY",
            "RESEARCH_AGENT_DEEPSEEK_API_KEY",
        ),
    )
    model_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    model_max_retries: int = Field(default=3, ge=0, le=10)
    model_input_cost_per_million_usd: float | None = Field(default=None, ge=0)
    model_output_cost_per_million_usd: float | None = Field(default=None, ge=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
