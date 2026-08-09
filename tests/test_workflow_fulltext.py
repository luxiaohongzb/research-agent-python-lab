from pathlib import Path

import pytest

from research_agent.domain import ParsedDocument, ResearchRequest
from research_agent.ingestion import TeiParser
from research_agent.providers import CompositePaperProvider, OfflinePaperProvider
from research_agent.retrieval import InMemoryHybridIndex, ResearchRetriever
from research_agent.workflow import ResearchWorkflow


@pytest.mark.asyncio
async def test_workflow_uses_full_text_when_metadata_has_no_matching_abstract() -> None:
    source = TeiParser().parse((Path(__file__).parent / "fixtures" / "sample.tei.xml").read_bytes())
    opaque_paper = source.paper.model_copy(
        update={"title": "Opaque Experiment", "abstract": "", "score": 0}
    )
    document = ParsedDocument(
        paper=opaque_paper,
        passages=source.passages,
        citation_edges=source.citation_edges,
        document_hash=source.document_hash,
        parser=source.parser,
        parser_version=source.parser_version,
    )
    index = InMemoryHybridIndex()
    await index.upsert(document)
    workflow = ResearchWorkflow(
        retriever=ResearchRetriever(
            metadata=CompositePaperProvider((OfflinePaperProvider((opaque_paper,)),)),
            index=index,
        )
    )

    result = await workflow.run(
        ResearchRequest(
            question="How does claim verification use an exact source passage?",
            max_iterations=1,
        )
    )

    assert result.evidence
    assert any(item.section == "Methods" for item in result.passages)
    assert any("exact source passage" in item.text for item in result.passages)
