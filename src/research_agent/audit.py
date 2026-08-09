from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=lambda: f"audit-{uuid4().hex}")
    tenant_id: str
    actor: str
    action: str
    run_id: str | None = None
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuditStore(Protocol):
    async def append(self, event: AuditEvent) -> None: ...

    async def list(self, tenant_id: str, *, limit: int = 100) -> tuple[AuditEvent, ...]: ...

    async def close(self) -> None: ...


class InMemoryAuditStore:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = asyncio.Lock()

    async def append(self, event: AuditEvent) -> None:
        async with self._lock:
            self._events.append(event)

    async def list(self, tenant_id: str, *, limit: int = 100) -> tuple[AuditEvent, ...]:
        async with self._lock:
            filtered = [item for item in self._events if item.tenant_id == tenant_id]
        return tuple(reversed(filtered[-limit:]))

    async def close(self) -> None:
        return None


class PostgresAuditStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def append(self, event: AuditEvent) -> None:
        pool = await self._ensure_pool()
        await pool.execute(
            """
            INSERT INTO audit_events
                (event_id, tenant_id, actor, action, run_id, details, created_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            """,
            event.event_id,
            event.tenant_id,
            event.actor,
            event.action,
            event.run_id,
            event.details,
            event.created_at,
        )

    async def list(self, tenant_id: str, *, limit: int = 100) -> tuple[AuditEvent, ...]:
        pool = await self._ensure_pool()
        rows = await pool.fetch(
            """
            SELECT event_id, tenant_id, actor, action, run_id, details, created_at
            FROM audit_events WHERE tenant_id = $1
            ORDER BY created_at DESC LIMIT $2
            """,
            tenant_id,
            limit,
        )
        return tuple(AuditEvent.model_validate(dict(row)) for row in rows)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                try:
                    asyncpg = importlib.import_module("asyncpg")
                except ImportError as exc:
                    raise RuntimeError('Audit store requires the "postgres" extra') from exc

                async def initialize(connection: Any) -> None:
                    await connection.set_type_codec(
                        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
                    )

                self._pool = await asyncpg.create_pool(self._dsn, init=initialize)
                await self._pool.execute(_SCHEMA)
        return self._pool


_SCHEMA = """
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
"""
