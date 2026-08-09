from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from RESEARCH_AGENT_* variables."""

    model_config = SettingsConfigDict(
        env_prefix="RESEARCH_AGENT_",
        env_file=".env",
        extra="ignore",
    )

    provider_mode: Literal["offline", "hybrid"] = "offline"
    openalex_email: str | None = None
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    default_max_papers: int = Field(default=8, ge=1, le=50)
    default_max_iterations: int = Field(default=2, ge=1, le=5)
    model: str = "gpt-5-mini"


@lru_cache
def get_settings() -> Settings:
    return Settings()
