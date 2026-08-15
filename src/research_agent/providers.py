from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import HttpUrl

from research_agent.domain import Paper, SearchTask, SourceScope

_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "by",
        "conflicting",
        "does",
        "evidence",
        "exist",
        "for",
        "from",
        "how",
        "improve",
        "in",
        "is",
        "it",
        "limitations",
        "main",
        "of",
        "on",
        "or",
        "question",
        "research",
        "supports",
        "the",
        "to",
        "what",
        "with",
    }
)


def tokenize(text: str) -> set[str]:
    latin = re.findall(r"[a-z0-9]+", text.lower())
    cjk_segments = re.findall(r"[\u4e00-\u9fff]+", text)
    cjk = [
        segment[index : index + 2]
        for segment in cjk_segments
        for index in range(max(1, len(segment) - 1))
    ]
    return {
        token
        for token in (*latin, *cjk)
        if (len(token) > 1 or token in cjk) and token not in _SEARCH_STOPWORDS
    }


def relevance(query: str, text: str) -> float:
    query_tokens = tokenize(query)
    if not query_tokens:
        return 0.0
    overlap = query_tokens & tokenize(text)
    return len(overlap) / len(query_tokens)


def stable_paper_id(*, doi: str | None, title: str) -> str:
    value = normalize_doi(doi) or normalize_title(title)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"paper-{digest}"


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", doi.strip().lower())


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", title.lower())


class PaperProvider(Protocol):
    name: str

    async def search(self, task: SearchTask, limit: int) -> list[Paper]: ...


@dataclass(frozen=True)
class SearchBatch:
    papers: tuple[Paper, ...]
    errors: tuple[str, ...]


class OfflinePaperProvider:
    name = "offline"

    def __init__(self, papers: tuple[Paper, ...] | None = None) -> None:
        self._papers = papers or default_offline_papers()

    async def search(self, task: SearchTask, limit: int) -> list[Paper]:
        ranked: list[Paper] = []
        for paper in self._papers:
            score = relevance(task.query, f"{paper.title} {paper.abstract}")
            if score <= 0:
                continue
            if task.year_from and paper.year and paper.year < task.year_from:
                continue
            if task.year_to and paper.year and paper.year > task.year_to:
                continue
            ranked.append(paper.model_copy(update={"score": score}))
        return sorted(ranked, key=lambda item: (-item.score, -(item.year or 0)))[:limit]


class OpenAlexPaperProvider:
    name = "openalex"
    _base_url = "https://api.openalex.org/works"

    def __init__(self, *, client: httpx.AsyncClient, email: str | None = None) -> None:
        self._client = client
        self._email = email

    async def search(self, task: SearchTask, limit: int) -> list[Paper]:
        params: dict[str, Any] = {"search": task.query, "per-page": min(limit, 25)}
        if self._email:
            params["mailto"] = self._email
        filters: list[str] = []
        if task.year_from:
            filters.append(f"from_publication_date:{task.year_from}-01-01")
        if task.year_to:
            filters.append(f"to_publication_date:{task.year_to}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        response = await self._client.get(self._base_url, params=params)
        response.raise_for_status()
        return [paper for item in response.json().get("results", []) if (paper := _openalex(item))]


class CrossrefPaperProvider:
    name = "crossref"
    _base_url = "https://api.crossref.org/works"

    def __init__(self, *, client: httpx.AsyncClient, email: str | None = None) -> None:
        self._client = client
        self._email = email

    async def search(self, task: SearchTask, limit: int) -> list[Paper]:
        params: dict[str, Any] = {
            "query.bibliographic": task.query,
            "rows": min(limit, 25),
            "select": "DOI,title,author,published,abstract,URL",
        }
        if self._email:
            params["mailto"] = self._email
        filters: list[str] = []
        if task.year_from:
            filters.append(f"from-pub-date:{task.year_from}-01-01")
        if task.year_to:
            filters.append(f"until-pub-date:{task.year_to}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        response = await self._client.get(self._base_url, params=params)
        response.raise_for_status()
        items = response.json().get("message", {}).get("items", [])
        return [paper for item in items if (paper := _crossref(item))]


class CompositePaperProvider:
    """Fan out to providers while isolating individual provider failures."""

    def __init__(self, providers: tuple[PaperProvider, ...]) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._providers = providers

    @property
    def provider_count(self) -> int:
        return len(self._providers)

    def providers_for(self, source_scope: SourceScope) -> tuple[PaperProvider, ...]:
        public = tuple(
            provider for provider in self._providers if _provider_kind(provider) == "public"
        )
        zotero = tuple(
            provider for provider in self._providers if _provider_kind(provider) == "zotero"
        )
        offline = tuple(
            provider for provider in self._providers if _provider_kind(provider) == "offline"
        )
        if source_scope is SourceScope.PUBLIC:
            return public
        if source_scope is SourceScope.ZOTERO:
            return zotero
        if source_scope is SourceScope.PRIVATE:
            return ()
        configured = (*public, *zotero)
        if source_scope is SourceScope.ALL:
            return configured
        # Demo fixtures are a fallback, never mixed into a real research corpus.
        return configured or offline

    def provider_count_for(self, source_scope: SourceScope) -> int:
        return len(self.providers_for(source_scope))

    def provider_names_for(self, source_scope: SourceScope) -> tuple[str, ...]:
        return tuple(provider.name for provider in self.providers_for(source_scope))

    async def search(
        self,
        task: SearchTask,
        limit: int,
        source_scope: SourceScope = SourceScope.AUTO,
    ) -> SearchBatch:
        providers = self.providers_for(source_scope)
        if not providers:
            return SearchBatch((), ())
        outcomes = await asyncio.gather(
            *(provider.search(task, limit) for provider in providers),
            return_exceptions=True,
        )
        papers: list[Paper] = []
        errors: list[str] = []
        for provider, outcome in zip(providers, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                errors.append(f"{provider.name}: {type(outcome).__name__}")
            else:
                papers.extend(outcome)
        return SearchBatch(tuple(papers), tuple(errors))


def _provider_kind(provider: PaperProvider) -> str:
    if provider.name in {"openalex", "crossref", "semantic_scholar"}:
        return "public"
    if provider.name.startswith("mcp:"):
        return "zotero"
    if provider.name == "offline":
        return "offline"
    return "public"


def deduplicate_papers(papers: list[Paper] | tuple[Paper, ...]) -> list[Paper]:
    selected: dict[str, Paper] = {}
    for paper in papers:
        key = (
            f"doi:{normalize_doi(paper.doi)}"
            if paper.doi
            else f"title:{normalize_title(paper.title)}"
        )
        current = selected.get(key)
        if current is None or _paper_rank(paper) > _paper_rank(current):
            selected[key] = paper
    return sorted(selected.values(), key=lambda item: (-item.score, -(item.year or 0)))


def _paper_rank(paper: Paper) -> tuple[float, int, int]:
    return paper.score, bool(paper.abstract), len(paper.external_ids)


def _openalex(item: dict[str, Any]) -> Paper | None:
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    doi = normalize_doi(item.get("doi"))
    authors = tuple(
        authorship.get("author", {}).get("display_name", "")
        for authorship in item.get("authorships", [])[:10]
        if authorship.get("author", {}).get("display_name")
    )
    abstract = _rebuild_abstract(item.get("abstract_inverted_index") or {})
    return Paper(
        paper_id=stable_paper_id(doi=doi, title=title),
        title=title,
        abstract=abstract,
        authors=authors,
        year=item.get("publication_year"),
        doi=doi,
        url=item.get("primary_location", {}).get("landing_page_url") or item.get("id"),
        source="openalex",
        external_ids={k: str(v) for k, v in (item.get("ids") or {}).items() if v},
        score=float(item.get("relevance_score") or 0),
    )


def _crossref(item: dict[str, Any]) -> Paper | None:
    titles = item.get("title") or []
    title = str(titles[0] if titles else "").strip()
    if not title:
        return None
    doi = normalize_doi(item.get("DOI"))
    date_parts = (item.get("published") or {}).get("date-parts") or [[]]
    year = date_parts[0][0] if date_parts and date_parts[0] else None
    authors = tuple(
        " ".join(filter(None, (author.get("given"), author.get("family"))))
        for author in (item.get("author") or [])[:10]
    )
    abstract = re.sub(r"<[^>]+>", " ", str(item.get("abstract") or "")).strip()
    return Paper(
        paper_id=stable_paper_id(doi=doi, title=title),
        title=title,
        abstract=abstract,
        authors=authors,
        year=year,
        doi=doi,
        url=item.get("URL"),
        source="crossref",
        external_ids={"doi": doi} if doi else {},
    )


def _rebuild_abstract(index: dict[str, list[int]]) -> str:
    positions = [(position, word) for word, values in index.items() for position in values]
    return " ".join(word for _, word in sorted(positions))


def default_offline_papers() -> tuple[Paper, ...]:
    records = (
        (
            "Agentic Retrieval-Augmented Generation for Scientific Synthesis",
            "Agentic RAG iteratively plans searches, retrieves scientific passages, and "
            "reflects on evidence coverage. Bounded retrieval loops improve coverage while "
            "explicit stopping conditions control cost and latency. 智能体检索通过有界循环提高"
            "科研综述覆盖度，并用停止条件控制成本。",
            2025,
            "10.5555/agentic-rag",
        ),
        (
            "Claim-Level Citation Verification in Long-Form Research Reports",
            "Claim-level verification links each atomic claim to exact source passages. "
            "Separating report generation from verification improves citation faithfulness "
            "and exposes unsupported or conflicting statements. 声明级引用核验把原子结论"
            "绑定到来源段落，并识别不受支持或冲突的陈述。",
            2026,
            "10.5555/claim-verification",
        ),
        (
            "Budget-Aware Multi-Agent Systems for Deep Research",
            "Parallel research workers increase breadth on decomposable questions, but "
            "coordination and token costs rise quickly. A complexity router should reserve "
            "multi-agent execution for high-value tasks with independent subquestions. "
            "预算感知的多智能体系统只为可分解的高价值任务启动并行研究者。",
            2026,
            "10.5555/budget-multi-agent",
        ),
        (
            "Evidence Cards: Provenance-Preserving Scientific Question Answering",
            "Evidence cards preserve paper identity, source passages, study context, "
            "limitations, and extraction metadata. Provenance-first data models make "
            "scientific answers auditable and enable deterministic citation rendering. "
            "证据卡片保存论文身份、来源段落、研究背景、局限与抽取元数据。",
            2025,
            "10.5555/evidence-cards",
        ),
    )
    return tuple(
        Paper(
            paper_id=stable_paper_id(doi=doi, title=title),
            title=title,
            abstract=abstract,
            authors=("Offline Fixture",),
            year=year,
            doi=doi,
            url=HttpUrl(f"https://example.org/papers/{index}"),
            source="offline",
            external_ids={"fixture": str(index)},
        )
        for index, (title, abstract, year, doi) in enumerate(records, start=1)
    )
