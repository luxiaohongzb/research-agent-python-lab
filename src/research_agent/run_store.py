from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any, Protocol

from research_agent.domain import RunSnapshot


class RunNotFoundError(KeyError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class RunStore(Protocol):
    async def create(self, snapshot: RunSnapshot) -> RunSnapshot: ...

    async def save(self, snapshot: RunSnapshot) -> None: ...

    async def get(self, run_id: str) -> RunSnapshot: ...

    async def close(self) -> None: ...


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, RunSnapshot] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create(self, snapshot: RunSnapshot) -> RunSnapshot:
        async with self._lock:
            if snapshot.idempotency_key:
                existing_id = self._idempotency.get(snapshot.idempotency_key)
                if existing_id:
                    existing = self._runs[existing_id]
                    _validate_idempotent_request(existing, snapshot)
                    return existing
                self._idempotency[snapshot.idempotency_key] = snapshot.run_id
            self._runs[snapshot.run_id] = snapshot
            return snapshot

    async def save(self, snapshot: RunSnapshot) -> None:
        async with self._lock:
            self._runs[snapshot.run_id] = snapshot

    async def get(self, run_id: str) -> RunSnapshot:
        async with self._lock:
            snapshot = self._runs.get(run_id)
        if snapshot is None:
            raise RunNotFoundError(run_id)
        return snapshot

    async def close(self) -> None:
        return None


class PostgresRunStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def create(self, snapshot: RunSnapshot) -> RunSnapshot:
        pool = await self._ensure_pool()
        payload = snapshot.model_dump(mode="json")
        async with pool.acquire() as connection:
            stored = await connection.fetchval(
                """
                INSERT INTO research_runs (run_id, idempotency_key, snapshot, updated_at)
                VALUES ($1, $2, $3::jsonb, now())
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING snapshot
                """,
                snapshot.run_id,
                snapshot.idempotency_key,
                payload,
            )
            if stored is None and snapshot.idempotency_key:
                stored = await connection.fetchval(
                    "SELECT snapshot FROM research_runs WHERE idempotency_key = $1",
                    snapshot.idempotency_key,
                )
        if stored is None:
            raise RuntimeError("failed to create run snapshot")
        existing = RunSnapshot.model_validate(stored)
        _validate_idempotent_request(existing, snapshot)
        return existing

    async def save(self, snapshot: RunSnapshot) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO research_runs (run_id, idempotency_key, snapshot, updated_at)
                VALUES ($1, $2, $3::jsonb, now())
                ON CONFLICT (run_id) DO UPDATE SET
                    snapshot = EXCLUDED.snapshot,
                    updated_at = now()
                """,
                snapshot.run_id,
                snapshot.idempotency_key,
                snapshot.model_dump(mode="json"),
            )

    async def get(self, run_id: str) -> RunSnapshot:
        pool = await self._ensure_pool()
        async with pool.acquire() as connection:
            stored = await connection.fetchval(
                "SELECT snapshot FROM research_runs WHERE run_id = $1", run_id
            )
        if stored is None:
            raise RunNotFoundError(run_id)
        return RunSnapshot.model_validate(stored)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is not None:
                return self._pool
            try:
                asyncpg = importlib.import_module("asyncpg")
            except ImportError as exc:
                raise RuntimeError(
                    'PostgreSQL run store requires: python -m pip install -e ".[postgres]"'
                ) from exc

            async def initialize(connection: Any) -> None:
                await connection.set_type_codec(
                    "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
                )

            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=1,
                max_size=8,
                init=initialize,
            )
            async with self._pool.acquire() as connection:
                await connection.execute(_SCHEMA)
        return self._pool


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_runs (
    run_id text PRIMARY KEY,
    idempotency_key text UNIQUE,
    snapshot jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS research_runs_updated_at_idx ON research_runs(updated_at DESC);
"""


def _validate_idempotent_request(existing: RunSnapshot, candidate: RunSnapshot) -> None:
    if existing.run_id != candidate.run_id and existing.request != candidate.request:
        raise IdempotencyConflictError(
            "idempotency key is already associated with a different request"
        )
