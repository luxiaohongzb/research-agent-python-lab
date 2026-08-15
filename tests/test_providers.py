import pytest

from research_agent.domain import Paper, SearchTask, SourceScope
from research_agent.providers import (
    CompositePaperProvider,
    OfflinePaperProvider,
    deduplicate_papers,
    stable_paper_id,
)


class NamedProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def search(self, task: SearchTask, limit: int) -> list[Paper]:
        return []


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


def test_composite_provider_routes_explicit_source_scopes() -> None:
    provider = CompositePaperProvider(
        (
            OfflinePaperProvider(),
            NamedProvider("openalex"),
            NamedProvider("crossref"),
            NamedProvider("mcp:zotero-mcp"),
        )
    )

    assert provider.provider_names_for(SourceScope.PUBLIC) == ("openalex", "crossref")
    assert provider.provider_names_for(SourceScope.ZOTERO) == ("mcp:zotero-mcp",)
    assert provider.provider_names_for(SourceScope.PRIVATE) == ()
    assert provider.provider_names_for(SourceScope.ALL) == (
        "openalex",
        "crossref",
        "mcp:zotero-mcp",
    )
    assert "offline" not in provider.provider_names_for(SourceScope.AUTO)


def test_composite_provider_uses_offline_only_as_auto_fallback() -> None:
    provider = CompositePaperProvider((OfflinePaperProvider(),))

    assert provider.provider_names_for(SourceScope.AUTO) == ("offline",)
    assert provider.provider_names_for(SourceScope.ALL) == ()
