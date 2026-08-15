from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable

import httpx
from pydantic import HttpUrl

from research_agent.domain import (
    CitationEdge,
    CitationRelation,
    Paper,
    ParsedDocument,
    Passage,
)
from research_agent.observability import observe_provider_attempt
from research_agent.providers import normalize_doi, stable_paper_id
from research_agent.resilience import RetryPolicy, retry_async

TEI = "{http://www.tei-c.org/ns/1.0}"
XML = "{http://www.w3.org/XML/1998/namespace}"


class GrobidError(RuntimeError):
    pass


class GrobidClient:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8070",
        timeout_seconds: float = 120,
        max_attempts: int = 3,
        initial_backoff_seconds: float = 0.25,
        max_backoff_seconds: float = 4.0,
        max_pdf_bytes: int = 30 * 1024 * 1024,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._retry_policy = RetryPolicy(
            max_attempts=max_attempts,
            initial_backoff_seconds=initial_backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
        )
        self._max_pdf_bytes = max_pdf_bytes
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    async def process_pdf(self, pdf_bytes: bytes, *, filename: str) -> bytes:
        if not pdf_bytes.startswith(b"%PDF-"):
            raise GrobidError("input is not a PDF document")
        if len(pdf_bytes) > self._max_pdf_bytes:
            raise GrobidError(f"PDF exceeds the {self._max_pdf_bytes}-byte limit")
        data = {
            "consolidateHeader": "0",
            "consolidateCitations": "0",
            "includeRawCitations": "1",
            "segmentSentences": "1",
            "generateIDs": "1",
            "teiCoordinates": ["head", "p", "figure", "biblStruct"],
        }

        async def request() -> bytes:
            response = await self._client.post(
                f"{self._base_url}/api/processFulltextDocument",
                data=data,
                files={"input": (filename, pdf_bytes, "application/pdf")},
            )
            if response.status_code == 200:
                return response.content
            if response.status_code == 204:
                raise GrobidError("GROBID extracted no content from the PDF")
            response.raise_for_status()
            raise GrobidError(f"GROBID returned unexpected HTTP {response.status_code}")

        try:
            return await retry_async(
                request,
                policy=self._retry_policy,
                observer=lambda attempt, duration, outcome, retrying: observe_provider_attempt(
                    "grobid",
                    "process_pdf",
                    attempt,
                    duration,
                    outcome,
                    retrying,
                ),
            )
        except httpx.HTTPStatusError as exc:
            raise GrobidError(f"GROBID returned HTTP {exc.response.status_code}") from exc


class TeiParser:
    def __init__(
        self, *, parser_version: str = "grobid-0.9.0", max_chunk_chars: int = 1800
    ) -> None:
        self._parser_version = parser_version
        self._max_chunk_chars = max_chunk_chars

    def parse(self, tei_xml: bytes | str) -> ParsedDocument:
        raw = tei_xml.encode("utf-8") if isinstance(tei_xml, str) else tei_xml
        document_hash = hashlib.sha256(raw).hexdigest()
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise GrobidError("invalid TEI XML") from exc
        header = root.find(f"{TEI}teiHeader")
        if header is None:
            raise GrobidError("TEI document has no header")
        title = _first_text(header, (f".//{TEI}titleStmt/{TEI}title", f".//{TEI}title"))
        if not title:
            raise GrobidError("TEI document has no title")
        doi = normalize_doi(_idno(header, "DOI"))
        paper_id = stable_paper_id(doi=doi, title=title)
        authors = tuple(
            author
            for node in header.findall(f".//{TEI}sourceDesc//{TEI}author")
            if (author := _person_name(node))
        )
        abstract = _first_text(header, (f".//{TEI}profileDesc/{TEI}abstract",)) or ""
        year = _year(header)
        paper = Paper(
            paper_id=paper_id,
            title=title,
            abstract=abstract,
            authors=authors,
            year=year,
            doi=doi,
            url=HttpUrl(f"https://doi.org/{doi}") if doi else None,
            source="grobid",
            external_ids={"document_sha256": document_hash},
        )
        passages = self._passages(root, paper_id, document_hash, abstract)
        edges = self._citation_edges(root, paper_id)
        return ParsedDocument(
            paper=paper,
            passages=passages,
            citation_edges=edges,
            document_hash=document_hash,
            parser="grobid",
            parser_version=self._parser_version,
        )

    def _passages(
        self,
        root: ET.Element,
        paper_id: str,
        document_hash: str,
        abstract: str,
    ) -> tuple[Passage, ...]:
        passages: list[Passage] = []
        cursor = 0

        def add_text(text: str, section: str, coords: tuple[str, ...] = ()) -> None:
            nonlocal cursor
            for chunk in _chunk_text(text, self._max_chunk_chars):
                start = cursor
                cursor += len(chunk)
                passages.append(
                    Passage(
                        paper_id=paper_id,
                        text=chunk,
                        section=section,
                        page=_page(coords),
                        start_char=start,
                        end_char=cursor,
                        coordinates=coords,
                        parser_version=self._parser_version,
                        content_hash=hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                    )
                )
                cursor += 1

        if abstract:
            add_text(abstract, "Abstract")
        body = root.find(f".//{TEI}text/{TEI}body")
        if body is not None:
            for child in body:
                if child.tag == f"{TEI}div":
                    self._walk_div(child, (), add_text)
                elif child.tag == f"{TEI}p":
                    add_text(_text(child), "Body", _coords(child))
        for figure in root.findall(f".//{TEI}figure"):
            if figure.get("type") != "table":
                continue
            table_text = _text(figure)
            if table_text:
                add_text(table_text, "Table", _coords(figure))
        unique: dict[tuple[str, str], Passage] = {}
        for passage in passages:
            unique[(passage.section, passage.content_hash or passage.passage_id)] = passage
        return tuple(unique.values())

    def _walk_div(
        self,
        div: ET.Element,
        parents: tuple[str, ...],
        add_text: Callable[[str, str, tuple[str, ...]], None],
    ) -> None:
        heading_node = div.find(f"{TEI}head")
        heading = _text(heading_node) if heading_node is not None else ""
        path = (*parents, heading) if heading else parents
        section = " > ".join(path) or "Body"
        for child in div:
            if child.tag == f"{TEI}p":
                text = _text(child)
                if text:
                    add_text(text, section, _coords(child))
            elif child.tag == f"{TEI}div":
                self._walk_div(child, path, add_text)

    def _citation_edges(self, root: ET.Element, source_paper_id: str) -> tuple[CitationEdge, ...]:
        edges: list[CitationEdge] = []
        for reference in root.findall(f".//{TEI}listBibl/{TEI}biblStruct"):
            title = _first_text(
                reference,
                (f".//{TEI}analytic/{TEI}title", f".//{TEI}monogr/{TEI}title"),
            )
            doi = normalize_doi(_idno(reference, "DOI"))
            if not title and not doi:
                continue
            target_id = stable_paper_id(doi=doi, title=title or doi or "unknown")
            edges.append(
                CitationEdge(
                    source_paper_id=source_paper_id,
                    target_paper_id=target_id,
                    relation=CitationRelation.REFERENCES,
                    source="grobid",
                )
            )
        return tuple(dict.fromkeys(edges))


def _first_text(node: ET.Element, paths: Iterable[str]) -> str | None:
    for path in paths:
        found = node.find(path)
        if found is not None and (value := _text(found)):
            return value
    return None


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", " ".join(node.itertext())).strip()


def _idno(node: ET.Element, kind: str) -> str | None:
    for item in node.findall(f".//{TEI}idno"):
        if (item.get("type") or "").upper() == kind.upper() and (value := _text(item)):
            return value
    return None


def _person_name(author: ET.Element) -> str:
    parts = [
        _text(item)
        for item in author.findall(f".//{TEI}forename") + author.findall(f".//{TEI}surname")
    ]
    return " ".join(filter(None, parts))


def _year(header: ET.Element) -> int | None:
    for date in header.findall(f".//{TEI}date"):
        value = date.get("when") or _text(date)
        if match := re.search(r"\b(19|20)\d{2}\b", value):
            return int(match.group())
    return None


def _coords(node: ET.Element) -> tuple[str, ...]:
    value = node.get("coords", "")
    return tuple(part for part in value.split(";") if part)


def _page(coords: tuple[str, ...]) -> int | None:
    if not coords:
        return None
    try:
        return int(coords[0].split(",", maxsplit=1)[0])
    except ValueError:
        return None


def _chunk_text(text: str, max_chars: int) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return ()
    if len(normalized) <= max_chars:
        return (normalized,)
    sentences = [
        item.strip() for item in re.split(r"(?<=[.!?。！？])\s+", normalized) if item.strip()
    ]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(
                sentence[index : index + max_chars] for index in range(0, len(sentence), max_chars)
            )
        elif not current:
            current = sentence
        elif len(current) + len(sentence) + 1 <= max_chars:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return tuple(chunks)
