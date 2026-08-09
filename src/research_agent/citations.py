from __future__ import annotations

import re
from typing import Any

from research_agent.domain import Paper


def render_bibtex(papers: tuple[Paper, ...]) -> str:
    entries: list[str] = []
    used_keys: set[str] = set()
    for paper in papers:
        key = _unique_key(_citation_key(paper), used_keys)
        fields = [
            ("title", paper.title),
            ("author", " and ".join(paper.authors)),
            ("year", str(paper.year) if paper.year else ""),
            ("doi", paper.doi or ""),
            ("url", str(paper.url) if paper.url else ""),
        ]
        body = ",\n".join(
            f"  {name} = {{{_bibtex_escape(value)}}}" for name, value in fields if value
        )
        entries.append(f"@article{{{key},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def render_csl_json(papers: tuple[Paper, ...]) -> list[dict[str, Any]]:
    return [
        {
            "id": paper.paper_id,
            "type": "article-journal",
            "title": paper.title,
            "author": [_csl_author(name) for name in paper.authors],
            **({"issued": {"date-parts": [[paper.year]]}} if paper.year else {}),
            **({"DOI": paper.doi} if paper.doi else {}),
            **({"URL": str(paper.url)} if paper.url else {}),
        }
        for paper in papers
    ]


def _citation_key(paper: Paper) -> str:
    family = paper.authors[0].split()[-1] if paper.authors else "anonymous"
    title_word = next(iter(re.findall(r"[A-Za-z0-9]+", paper.title)), "work")
    return re.sub(r"[^A-Za-z0-9]", "", f"{family}{paper.year or 'nd'}{title_word}")


def _unique_key(key: str, used: set[str]) -> str:
    candidate = key
    suffix = 2
    while candidate.lower() in used:
        candidate = f"{key}{suffix}"
        suffix += 1
    used.add(candidate.lower())
    return candidate


def _bibtex_escape(value: str) -> str:
    return value.replace("\\", "\\textbackslash{}").replace("{", "\\{").replace("}", "\\}")


def _csl_author(name: str) -> dict[str, str]:
    parts = name.split()
    if len(parts) < 2:
        return {"literal": name}
    return {"family": parts[-1], "given": " ".join(parts[:-1])}
