import httpx
import pytest

from research_agent.domain import SearchTask
from research_agent.semantic_scholar import SemanticScholarProvider


def _record(paper_id: str, title: str, doi: str) -> dict[str, object]:
    return {
        "paperId": paper_id,
        "title": title,
        "abstract": "Evidence-grounded retrieval.",
        "authors": [{"name": "Ada Lin"}],
        "year": 2026,
        "url": f"https://example.org/{paper_id}",
        "externalIds": {"DOI": doi},
    }


@pytest.mark.asyncio
async def test_search_and_citation_graph_expansion() -> None:
    seed = _record("S1", "Seed paper", "10.1/seed")
    cited_by = _record("S2", "Citing paper", "10.1/citing")
    reference = _record("S3", "Referenced paper", "10.1/reference")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/paper/search"):
            assert request.headers["x-api-key"] == "test-key"
            return httpx.Response(200, json={"data": [seed]})
        if request.url.path.endswith("/citations"):
            return httpx.Response(200, json={"data": [{"citingPaper": cited_by}]})
        if request.url.path.endswith("/references"):
            return httpx.Response(200, json={"data": [{"citedPaper": reference}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = SemanticScholarProvider(client=client, api_key="test-key")
        papers = await provider.search(
            SearchTask(
                sub_question="retrieval",
                query="evidence retrieval",
                purpose="test",
                year_from=2024,
            ),
            limit=5,
        )
        neighbors = await provider.expand(papers[0], limit=4)

    assert papers[0].external_ids["semanticScholar"] == "S1"
    assert {item.external_ids["semanticScholar"] for item in neighbors} == {"S2", "S3"}
