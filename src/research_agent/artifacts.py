from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import uuid4

from research_agent.domain import ArtifactKind, ArtifactRef, Paper, Passage, RetrievalHit
from research_agent.retrieval import RetrievalBatch


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactStore(Protocol):
    async def put(self, *, run_id: str, kind: ArtifactKind, value: Any) -> ArtifactRef: ...

    async def get(self, artifact_id: str, *, run_id: str) -> Any: ...

    async def delete_run(self, run_id: str) -> int: ...

    async def count(self, *, run_id: str | None = None) -> int: ...


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


class S3ArtifactStore:
    """JSON-only Artifact Store compatible with AWS S3 and MinIO."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        prefix: str = "artifacts",
        client: Any = None,
    ) -> None:
        self._bucket = bucket
        self._endpoint_url = endpoint_url
        self._region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._prefix = prefix.strip("/")
        self._client = client
        self._ready = False
        self._lock = asyncio.Lock()

    async def put(self, *, run_id: str, kind: ArtifactKind, value: Any) -> ArtifactRef:
        await self._ensure_bucket()
        reference = ArtifactRef(
            artifact_id=f"artifact-{uuid4().hex}",
            run_id=run_id,
            kind=kind,
        )
        payload = json.dumps(
            {
                "schema_version": 1,
                "reference": reference.model_dump(mode="json"),
                "value": _encode_value(kind, value),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=self._key(reference),
            Body=payload,
            ContentType="application/json",
        )
        return reference

    async def get(self, artifact_id: str, *, run_id: str) -> Any:
        await self._ensure_bucket()
        key = self._key_from_parts(run_id, artifact_id)
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self._bucket,
                Key=key,
            )
        except Exception as exc:
            if _s3_error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                raise ArtifactNotFoundError(artifact_id) from exc
            raise
        body = await asyncio.to_thread(response["Body"].read)
        payload = json.loads(body)
        reference = ArtifactRef.model_validate(payload["reference"])
        if reference.artifact_id != artifact_id or reference.run_id != run_id:
            raise ArtifactNotFoundError(artifact_id)
        return _decode_value(reference.kind, payload["value"])

    async def delete_run(self, run_id: str) -> int:
        await self._ensure_bucket()
        prefix = f"{self._prefix}/{run_id}/"
        keys = await self._list_keys(prefix)
        for offset in range(0, len(keys), 1_000):
            batch = keys[offset : offset + 1_000]
            await asyncio.to_thread(
                self._client.delete_objects,
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
        return len(keys)

    async def count(self, *, run_id: str | None = None) -> int:
        await self._ensure_bucket()
        prefix = f"{self._prefix}/{run_id}/" if run_id else f"{self._prefix}/"
        return len(await self._list_keys(prefix))

    async def _ensure_bucket(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            if self._client is None:
                try:
                    boto3 = importlib.import_module("boto3")
                except ImportError as exc:
                    raise RuntimeError(
                        'S3 artifacts require: python -m pip install -e ".[distributed]"'
                    ) from exc
                self._client = boto3.client(
                    "s3",
                    endpoint_url=self._endpoint_url,
                    region_name=self._region,
                    aws_access_key_id=self._access_key_id,
                    aws_secret_access_key=self._secret_access_key,
                )
            try:
                await asyncio.to_thread(self._client.head_bucket, Bucket=self._bucket)
            except Exception as exc:
                if _s3_error_code(exc) not in {"404", "NoSuchBucket", "NotFound"}:
                    raise
                arguments: dict[str, Any] = {"Bucket": self._bucket}
                if self._region != "us-east-1" and self._endpoint_url is None:
                    arguments["CreateBucketConfiguration"] = {"LocationConstraint": self._region}
                await asyncio.to_thread(self._client.create_bucket, **arguments)
            self._ready = True

    async def _list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            arguments: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": prefix,
                "MaxKeys": 1_000,
            }
            if token:
                arguments["ContinuationToken"] = token
            response = await asyncio.to_thread(self._client.list_objects_v2, **arguments)
            keys.extend(item["Key"] for item in response.get("Contents", ()))
            token = response.get("NextContinuationToken")
            if not token:
                return keys

    def _key(self, reference: ArtifactRef) -> str:
        return self._key_from_parts(reference.run_id, reference.artifact_id)

    def _key_from_parts(self, run_id: str, artifact_id: str) -> str:
        return f"{self._prefix}/{run_id}/{artifact_id}.json"


def _encode_value(kind: ArtifactKind, value: Any) -> dict[str, Any]:
    if kind is not ArtifactKind.RETRIEVAL_BATCH or not isinstance(value, RetrievalBatch):
        raise TypeError(f"unsupported artifact payload for {kind.value}")
    return {
        "papers": [item.model_dump(mode="json") for item in value.papers],
        "passages": [item.model_dump(mode="json") for item in value.passages],
        "hits": [item.model_dump(mode="json") for item in value.hits],
        "errors": list(value.errors),
    }


def _decode_value(kind: ArtifactKind, value: Mapping[str, Any]) -> Any:
    if kind is not ArtifactKind.RETRIEVAL_BATCH:
        raise TypeError(f"unsupported artifact kind {kind.value}")
    return RetrievalBatch(
        papers=tuple(Paper.model_validate(item) for item in value["papers"]),
        passages=tuple(Passage.model_validate(item) for item in value["passages"]),
        hits=tuple(RetrievalHit.model_validate(item) for item in value["hits"]),
        errors=tuple(str(item) for item in value["errors"]),
    )


def _s3_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return None
    error = response.get("Error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("Code")
    return str(code) if code is not None else None
