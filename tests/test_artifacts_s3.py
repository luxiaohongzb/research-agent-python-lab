from __future__ import annotations

import os
from io import BytesIO
from typing import Any
from uuid import uuid4

import pytest

from research_agent.artifacts import ArtifactNotFoundError, S3ArtifactStore
from research_agent.domain import ArtifactKind, Paper, Passage
from research_agent.retrieval import RetrievalBatch


class FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}

    def head_bucket(self, *, Bucket: str) -> None:
        if Bucket not in self.buckets:
            raise FakeS3Error("404")

    def create_bucket(self, *, Bucket: str, **_: Any) -> None:
        self.buckets.add(Bucket)

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **_: Any) -> None:
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, BytesIO]:
        try:
            value = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise FakeS3Error("NoSuchKey") from exc
        return {"Body": BytesIO(value)}

    def list_objects_v2(self, *, Bucket: str, Prefix: str, **_: Any) -> dict[str, object]:
        return {
            "Contents": [
                {"Key": key}
                for bucket, key in self.objects
                if bucket == Bucket and key.startswith(Prefix)
            ]
        }

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> None:
        for item in Delete["Objects"]:
            self.objects.pop((Bucket, item["Key"]), None)


@pytest.mark.asyncio
async def test_s3_artifact_store_round_trip_and_run_isolation() -> None:
    paper = Paper(
        paper_id="paper-s3",
        title="Persistent research artifacts",
        abstract="Artifacts remain available across worker processes.",
        source="fixture",
    )
    passage = Passage(
        paper_id=paper.paper_id,
        text=paper.abstract,
        end_char=len(paper.abstract),
    )
    batch = RetrievalBatch((paper,), (passage,), (), ())
    store = S3ArtifactStore(bucket="artifacts", client=FakeS3Client())

    reference = await store.put(
        run_id="run-one",
        kind=ArtifactKind.RETRIEVAL_BATCH,
        value=batch,
    )
    restored = await store.get(reference.artifact_id, run_id="run-one")

    assert restored == batch
    assert await store.count(run_id="run-one") == 1
    with pytest.raises(ArtifactNotFoundError):
        await store.get(reference.artifact_id, run_id="run-two")
    assert await store.delete_run("run-one") == 1
    assert await store.count() == 0


@pytest.mark.asyncio
async def test_s3_artifact_store_with_real_minio() -> None:
    endpoint = os.getenv("S3_TEST_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("S3_TEST_ENDPOINT_URL is not configured")
    import boto3

    bucket = f"research-test-{uuid4().hex}"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=os.getenv("S3_TEST_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.getenv("S3_TEST_SECRET_ACCESS_KEY", "minioadmin"),
    )
    paper = Paper(
        paper_id="paper-minio",
        title="MinIO artifact integration",
        abstract="The integration uses the S3 API.",
        source="fixture",
    )
    batch = RetrievalBatch(
        (paper,),
        (Passage(paper_id=paper.paper_id, text=paper.abstract, end_char=len(paper.abstract)),),
        (),
        (),
    )
    store = S3ArtifactStore(bucket=bucket, endpoint_url=endpoint, client=client)
    try:
        reference = await store.put(
            run_id="minio-run",
            kind=ArtifactKind.RETRIEVAL_BATCH,
            value=batch,
        )
        assert await store.get(reference.artifact_id, run_id="minio-run") == batch
        assert await store.delete_run("minio-run") == 1
    finally:
        client.delete_bucket(Bucket=bucket)
