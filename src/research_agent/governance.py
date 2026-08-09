from __future__ import annotations

from dataclasses import dataclass

from research_agent.domain import ResearchRequest
from research_agent.run_store import RunStore


class QuotaExceededError(RuntimeError):
    pass


@dataclass(frozen=True)
class TenantQuota:
    max_active_runs: int = 5
    max_workers: int = 5
    max_total_tokens: int = 1_000_000
    max_cost_usd: float = 100.0


class QuotaPolicy:
    def __init__(self, store: RunStore, quota: TenantQuota) -> None:
        self._store = store
        self._quota = quota

    async def enforce(self, tenant_id: str, request: ResearchRequest) -> None:
        if request.max_workers > self._quota.max_workers:
            raise QuotaExceededError("requested workers exceed the tenant quota")
        if request.max_total_tokens > self._quota.max_total_tokens:
            raise QuotaExceededError("requested tokens exceed the tenant quota")
        if request.max_cost_usd > self._quota.max_cost_usd:
            raise QuotaExceededError("requested cost exceeds the tenant quota")
        active = await self._store.count_active(tenant_id)
        if active >= self._quota.max_active_runs:
            raise QuotaExceededError("tenant active-run quota is exhausted")
