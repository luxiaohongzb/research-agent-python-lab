from pathlib import Path

import httpx
import pytest

from research_agent.domain import CitationRelation
from research_agent.ingestion import GrobidClient, GrobidError, TeiParser

FIXTURE = Path(__file__).parent / "fixtures" / "sample.tei.xml"


def test_tei_parser_preserves_sections_coordinates_and_citations() -> None:
    document = TeiParser().parse(FIXTURE.read_bytes())

    assert document.paper.title == "Evidence-Grounded Scientific Agents"
    assert document.paper.doi == "10.1234/agent.2026.1"
    assert document.paper.authors == ("Ada Lin",)
    assert document.paper.year == 2026
    methods = next(item for item in document.passages if item.section == "Methods")
    retrieval = next(item for item in document.passages if item.section == "Methods > Retrieval")
    assert methods.page == 2
    assert methods.coordinates == ("2,10.0,20.0,300.0,40.0",)
    assert retrieval.page == 3
    assert document.citation_edges[0].relation is CitationRelation.REFERENCES


@pytest.mark.asyncio
async def test_grobid_client_posts_pdf_and_returns_tei() -> None:
    tei = FIXTURE.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/processFulltextDocument"
        assert "multipart/form-data" in request.headers["content-type"]
        return httpx.Response(200, content=tei)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await GrobidClient(base_url="http://grobid", client=client).process_pdf(
            b"%PDF-1.7 fixture", filename="paper.pdf"
        )

    assert result == tei


@pytest.mark.asyncio
async def test_grobid_client_rejects_non_pdf() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        grobid = GrobidClient(client=client)
        with pytest.raises(GrobidError, match="not a PDF"):
            await grobid.process_pdf(b"plain text", filename="paper.pdf")


@pytest.mark.asyncio
async def test_grobid_client_retries_transient_service_failure() -> None:
    calls = 0
    tei = FIXTURE.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, content=tei)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await GrobidClient(
            base_url="http://grobid",
            client=client,
            initial_backoff_seconds=0,
        ).process_pdf(b"%PDF-1.7 fixture", filename="paper.pdf")

    assert result == tei
    assert calls == 2
