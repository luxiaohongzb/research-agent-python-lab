from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from pydantic import HttpUrl

from research_agent.domain import Paper, SearchTask
from research_agent.providers import normalize_doi, stable_paper_id


class SemanticScholarProvider:
    """Semantic Scholar Graph API search and citation-neighborhood adapter."""

    name = "semantic_scholar"
    _base_url = "https://api.semanticscholar.org/graph/v1"
    _fields = "paperId,title,abstract,authors,year,url,externalIds"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: str | None = None,
    ) -> None:
        self._client = client
        self._headers = {"x-api-key": api_key} if api_key else {}

    async def search(self, task: SearchTask, limit: int) -> list[Paper]:
        response = await self._client.get(
            f"{self._base_url}/paper/search",
            params={
                "query": task.query,
                "limit": min(limit, 100),
                "fields": self._fields,
                **({"year": _year_filter(task)} if task.year_from or task.year_to else {}),
            },
            headers=self._headers,
        )
        response.raise_for_status()
        papers = [paper for item in response.json().get("data", []) if (paper := _paper(item))]
        return [
            paper.model_copy(update={"score": 1 / rank})
            for rank, paper in enumerate(papers, start=1)
        ]

    async def expand(self, seed: Paper, limit: int) -> list[Paper]:
        identifier = _identifier(seed)
        if identifier is None or limit <= 0:
            return []
        encoded = quote(identifier, safe=":")
        per_lane = max(1, min(1000, (limit + 1) // 2))
        common = {"limit": per_lane, "fields": self._fields}
        citations, references = await _gather_responses(
            self._client,
            (
                f"{self._base_url}/paper/{encoded}/citations",
                f"{self._base_url}/paper/{encoded}/references",
            ),
            params=common,
            headers=self._headers,
        )
        expanded: list[Paper] = []
        for item in citations.json().get("data", []):
            if paper := _paper(item.get("citingPaper") or {}):
                expanded.append(paper)
        for item in references.json().get("data", []):
            if paper := _paper(item.get("citedPaper") or {}):
                expanded.append(paper)
        seen: set[str] = {seed.paper_id}
        unique: list[Paper] = []
        for paper in expanded:
            if paper.paper_id in seen:
                continue
            seen.add(paper.paper_id)
            unique.append(paper)
        return unique[:limit]


async def _gather_responses(
    client: httpx.AsyncClient,
    urls: tuple[str, str],
    *,
    params: dict[str, Any],
    headers: dict[str, str],
) -> tuple[httpx.Response, httpx.Response]:
    import asyncio

    left, right = await asyncio.gather(
        *(client.get(url, params=params, headers=headers) for url in urls)
    )
    left.raise_for_status()
    right.raise_for_status()
    return left, right


def _paper(item: dict[str, Any]) -> Paper | None:
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    external = item.get("externalIds") or {}
    doi = normalize_doi(external.get("DOI"))
    semantic_id = str(item.get("paperId") or "").strip()
    url = item.get("url")
    return Paper(
        paper_id=stable_paper_id(doi=doi, title=title),
        title=title,
        abstract=str(item.get("abstract") or ""),
        authors=tuple(
            str(author.get("name"))
            for author in (item.get("authors") or [])[:20]
            if author.get("name")
        ),
        year=item.get("year"),
        doi=doi,
        url=HttpUrl(url) if url else None,
        source="semantic_scholar",
        external_ids={
            **{str(key): str(value) for key, value in external.items() if value},
            **({"semanticScholar": semantic_id} if semantic_id else {}),
        },
    )


def _identifier(paper: Paper) -> str | None:
    semantic_id = paper.external_ids.get("semanticScholar")
    if semantic_id:
        return semantic_id
    if paper.doi:
        return f"DOI:{paper.doi}"
    return None


def _year_filter(task: SearchTask) -> str:
    if task.year_from and task.year_to:
        return f"{task.year_from}-{task.year_to}"
    if task.year_from:
        return f"{task.year_from}-"
    return f"-{task.year_to}"
