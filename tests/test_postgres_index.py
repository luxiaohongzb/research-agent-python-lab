import os
from pathlib import Path

import pytest

from research_agent.ingestion import TeiParser
from research_agent.postgres_index import PostgresHybridIndex


@pytest.mark.asyncio
async def test_postgres_hybrid_index_round_trip() -> None:
    dsn = os.getenv("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.skip("POSTGRES_TEST_DSN is not configured")
    document = TeiParser().parse(
        (Path(__file__).parent / "fixtures" / "sample.tei.xml").read_bytes()
    )
    index = PostgresHybridIndex(dsn)
    try:
        await index.upsert(document)
        hits = await index.search("reciprocal rank fusion vector lexical", limit=3)
    finally:
        await index.close()

    assert hits
    assert hits[0].passage.section == "Methods > Retrieval"
    assert hits[0].paper.paper_id == document.paper.paper_id
