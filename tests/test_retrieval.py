from pathlib import Path

import pytest

from research_agent.domain import RetrievalLane
from research_agent.ingestion import TeiParser
from research_agent.retrieval import InMemoryHybridIndex, reciprocal_rank_fusion

FIXTURE = Path(__file__).parent / "fixtures" / "sample.tei.xml"


def test_rrf_rewards_items_found_by_multiple_lanes() -> None:
    result = reciprocal_rank_fusion(
        {
            RetrievalLane.KEYWORD: ["lexical-only", "both"],
            RetrievalLane.VECTOR: ["both", "vector-only"],
        },
        k=60,
    )

    assert result[0][0] == "both"


@pytest.mark.asyncio
async def test_in_memory_hybrid_index_returns_traceable_passage() -> None:
    document = TeiParser().parse(FIXTURE.read_bytes())
    index = InMemoryHybridIndex()
    await index.upsert(document)

    hits = await index.search("reciprocal rank fusion vector lexical", limit=3)

    assert hits
    assert hits[0].passage.section == "Methods > Retrieval"
    assert RetrievalLane.KEYWORD in hits[0].lane_ranks
    assert hits[0].paper.paper_id == document.paper.paper_id
