from pathlib import Path

import pytest

from research_agent.domain import Paper, RetrievalLane, SearchTask, SourceScope
from research_agent.ingestion import TeiParser
from research_agent.providers import CompositePaperProvider
from research_agent.retrieval import (
    InMemoryHybridIndex,
    ResearchRetriever,
    reciprocal_rank_fusion,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample.tei.xml"


class PublicFixtureProvider:
    name = "openalex"

    async def search(self, task: SearchTask, limit: int) -> list[Paper]:
        return [
            Paper(
                paper_id="public-paper",
                title="Public metadata result",
                abstract="A public abstract about reciprocal rank fusion.",
                source=self.name,
                score=1,
            )
        ][:limit]


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


@pytest.mark.asyncio
async def test_retriever_keeps_public_and_private_scopes_separate() -> None:
    document = TeiParser().parse(FIXTURE.read_bytes())
    index = InMemoryHybridIndex()
    await index.upsert(document)
    retriever = ResearchRetriever(
        metadata=CompositePaperProvider((PublicFixtureProvider(),)),
        index=index,
    )
    task = SearchTask(
        sub_question="retrieval",
        query="reciprocal rank fusion vector lexical",
        purpose="scope test",
    )

    public = await retriever.search(task, 5, SourceScope.PUBLIC)
    private = await retriever.search(task, 5, SourceScope.PRIVATE)
    combined = await retriever.search(task, 5, SourceScope.ALL)

    assert {paper.source for paper in public.papers} == {"openalex"}
    assert public.passages == ()
    assert {paper.source for paper in private.papers} == {document.paper.source}
    assert private.passages
    assert {paper.source for paper in combined.papers} == {
        "openalex",
        document.paper.source,
    }
