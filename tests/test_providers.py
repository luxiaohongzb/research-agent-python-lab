import pytest

from research_agent.domain import Paper, SearchTask
from research_agent.providers import OfflinePaperProvider, deduplicate_papers, stable_paper_id


def test_deduplicate_prefers_richer_doi_record() -> None:
    title = "An Example Paper"
    sparse = Paper(
        paper_id=stable_paper_id(doi="10.1/example", title=title),
        title=title,
        doi="10.1/example",
        source="crossref",
    )
    rich = sparse.model_copy(
        update={
            "abstract": "A useful abstract.",
            "source": "openalex",
            "external_ids": {"doi": "10.1/example", "openalex": "W1"},
        }
    )

    result = deduplicate_papers([sparse, rich])

    assert result == [rich]


@pytest.mark.asyncio
async def test_offline_provider_does_not_return_irrelevant_fixtures() -> None:
    task = SearchTask(sub_question="x", query="zzzyyyxxx", purpose="negative test")

    result = await OfflinePaperProvider().search(task, limit=10)

    assert result == []
