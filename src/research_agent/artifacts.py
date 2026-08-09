from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from research_agent.domain import ArtifactKind, ArtifactRef


class ArtifactNotFoundError(LookupError):
    pass


class InMemoryArtifactStore:
    """Run-scoped object handoff for parallel workers.

    Graph state receives only ArtifactRef values. Large retrieval batches stay outside
    checkpoints and model context.
    """

    def __init__(self, *, max_artifacts: int = 5_000) -> None:
        self._max_artifacts = max_artifacts
        self._items: dict[str, tuple[ArtifactRef, Any]] = {}
        self._lock = asyncio.Lock()

    async def put(self, *, run_id: str, kind: ArtifactKind, value: Any) -> ArtifactRef:
        async with self._lock:
            if len(self._items) >= self._max_artifacts:
                raise RuntimeError("artifact store capacity exhausted")
            reference = ArtifactRef(
                artifact_id=f"artifact-{uuid4().hex}",
                run_id=run_id,
                kind=kind,
            )
            self._items[reference.artifact_id] = (reference, value)
            return reference

    async def get(self, artifact_id: str, *, run_id: str) -> Any:
        async with self._lock:
            stored = self._items.get(artifact_id)
            if stored is None or stored[0].run_id != run_id:
                raise ArtifactNotFoundError(artifact_id)
            return stored[1]

    async def delete_run(self, run_id: str) -> int:
        async with self._lock:
            selected = [
                key for key, (reference, _) in self._items.items() if reference.run_id == run_id
            ]
            for key in selected:
                del self._items[key]
            return len(selected)

    async def count(self, *, run_id: str | None = None) -> int:
        async with self._lock:
            if run_id is None:
                return len(self._items)
            return sum(reference.run_id == run_id for reference, _ in self._items.values())
