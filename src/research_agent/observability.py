from __future__ import annotations

import asyncio
import importlib
from contextlib import AbstractContextManager, nullcontext
from typing import Any, cast

from prometheus_client import Counter, Gauge, Histogram
from pydantic import BaseModel, ConfigDict, Field

from research_agent.domain import ResearchResult

RUNS = Counter(
    "research_agent_runs_total",
    "Completed research runs by terminal status.",
    ("status",),
)
RUN_DURATION = Histogram(
    "research_agent_run_duration_seconds",
    "End-to-end research run duration.",
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
RUN_COST = Histogram(
    "research_agent_estimated_cost_usd",
    "Estimated model cost per completed run.",
    buckets=(0, 0.001, 0.01, 0.1, 1, 5, 20, 100),
)
ACTIVE_RUNS = Gauge("research_agent_active_runs", "Currently executing asynchronous runs.")
WORKERS = Counter(
    "research_agent_workers_total",
    "Research workers by terminal status.",
    ("status",),
)


class RuntimeSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_runs: int = Field(ge=0)
    active_runs: int = Field(ge=0)
    status_counts: dict[str, int]
    total_papers: int = Field(ge=0)
    total_claims: int = Field(ge=0)
    total_workers: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0)


class RuntimeObservability:
    def __init__(self, *, otel_enabled: bool = False, service_name: str = "research-agent") -> None:
        self._lock = asyncio.Lock()
        self._active = 0
        self._total = 0
        self._status_counts: dict[str, int] = {}
        self._papers = 0
        self._claims = 0
        self._workers = 0
        self._cost = 0.0
        self._tracer: Any = _configure_tracer(service_name) if otel_enabled else None

    async def started(self) -> None:
        async with self._lock:
            self._active += 1
        ACTIVE_RUNS.inc()

    async def finished(self, result: ResearchResult) -> None:
        status = result.status.value
        async with self._lock:
            self._active = max(0, self._active - 1)
            self._total += 1
            self._status_counts[status] = self._status_counts.get(status, 0) + 1
            self._papers += len(result.papers)
            self._claims += len(result.claims)
            self._workers += len(result.workers)
            self._cost += result.budget.estimated_cost_usd
        ACTIVE_RUNS.dec()
        RUNS.labels(status=status).inc()
        RUN_DURATION.observe(result.budget.elapsed_ms / 1_000)
        RUN_COST.observe(result.budget.estimated_cost_usd)
        for worker in result.workers:
            WORKERS.labels(status=worker.status.value).inc()

    async def failed(self, status: str) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)
            self._total += 1
            self._status_counts[status] = self._status_counts.get(status, 0) + 1
        ACTIVE_RUNS.dec()
        RUNS.labels(status=status).inc()

    async def summary(self) -> RuntimeSummary:
        async with self._lock:
            return RuntimeSummary(
                total_runs=self._total,
                active_runs=self._active,
                status_counts=dict(self._status_counts),
                total_papers=self._papers,
                total_claims=self._claims,
                total_workers=self._workers,
                estimated_cost_usd=self._cost,
            )

    def span(self, name: str, attributes: dict[str, object]) -> AbstractContextManager[Any]:
        if self._tracer is None:
            return nullcontext()
        return cast(
            AbstractContextManager[Any],
            self._tracer.start_as_current_span(name, attributes=attributes),
        )


def _configure_tracer(service_name: str) -> Any:
    try:
        trace = importlib.import_module("opentelemetry.trace")
        resources = importlib.import_module("opentelemetry.sdk.resources")
        sdk_trace = importlib.import_module("opentelemetry.sdk.trace")
        export = importlib.import_module("opentelemetry.sdk.trace.export")
        otlp = importlib.import_module("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")
    except ImportError as exc:
        raise RuntimeError(
            'OpenTelemetry requires: python -m pip install -e ".[observability]"'
        ) from exc
    provider = sdk_trace.TracerProvider(
        resource=resources.Resource.create({"service.name": service_name})
    )
    provider.add_span_processor(export.BatchSpanProcessor(otlp.OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)
