from __future__ import annotations

import re
from typing import Protocol

from research_agent.domain import (
    AtomicClaim,
    Complexity,
    EvidenceCard,
    EvidenceType,
    Paper,
    Passage,
    ReportSection,
    ResearchPlan,
    ResearchReport,
    ResearchRequest,
    SearchTask,
    VerificationResult,
    VerificationStatus,
)
from research_agent.providers import relevance, tokenize


class ResearchReasoner(Protocol):
    async def plan(self, request: ResearchRequest) -> ResearchPlan: ...

    async def extract_evidence(
        self, question: str, paper: Paper, passage: Passage
    ) -> EvidenceCard | None: ...

    async def synthesize(
        self,
        question: str,
        papers: tuple[Paper, ...],
        evidence: tuple[EvidenceCard, ...],
    ) -> tuple[ResearchReport, tuple[AtomicClaim, ...]]: ...

    async def verify(
        self,
        claim: AtomicClaim,
        evidence: tuple[EvidenceCard, ...],
        passages: tuple[Passage, ...],
    ) -> VerificationResult: ...


class DeterministicReasoner:
    """A reproducible baseline. Replace it with a structured-output LLM adapter."""

    async def plan(self, request: ResearchRequest) -> ResearchPlan:
        deep_markers = ("compare", "contrast", "review", "比较", "综述", "路线", "现状")
        complexity = (
            Complexity.DEEP
            if any(marker in request.question.lower() for marker in deep_markers)
            or len(tokenize(request.question)) >= 12
            else Complexity.SIMPLE
        )
        sub_questions = [request.question]
        if complexity is Complexity.DEEP:
            sub_questions.extend(
                (
                    f"What evidence supports the main approaches in: {request.question}",
                    f"What limitations or conflicting evidence exist for: {request.question}",
                )
            )
        tasks = tuple(
            SearchTask(
                sub_question=sub_question,
                query=sub_question,
                purpose="core evidence" if index == 0 else "coverage and counter-evidence",
                year_from=request.year_from,
                year_to=request.year_to,
            )
            for index, sub_question in enumerate(sub_questions)
        )
        return ResearchPlan(
            complexity=complexity,
            objective=request.question,
            sub_questions=tuple(sub_questions),
            search_tasks=tasks,
            inclusion_criteria=(
                "Directly relevant to the research question",
                "Traceable source metadata",
            ),
            exclusion_criteria=("Missing title", "No retrievable evidence text"),
        )

    async def extract_evidence(
        self, question: str, paper: Paper, passage: Passage
    ) -> EvidenceCard | None:
        sentences = [
            part.strip() for part in re.split(r"(?<=[.!?。！？])\s*", passage.text) if part.strip()
        ]
        if not sentences:
            return None
        best = max(sentences, key=lambda sentence: relevance(question, sentence))
        score = relevance(question, best)
        if score == 0:
            best = sentences[0]
            score = relevance(question, f"{paper.title} {best}")
        return EvidenceCard(
            paper_id=paper.paper_id,
            passage_ids=(passage.passage_id,),
            atomic_finding=best,
            evidence_type=EvidenceType.DIRECT if score >= 0.1 else EvidenceType.BACKGROUND,
            limitations=("Offline baseline extracts abstract-level evidence only.",),
            source_quality="MEDIUM",
            confidence=min(0.95, max(0.55, score + 0.5)),
        )

    async def synthesize(
        self,
        question: str,
        papers: tuple[Paper, ...],
        evidence: tuple[EvidenceCard, ...],
    ) -> tuple[ResearchReport, tuple[AtomicClaim, ...]]:
        paper_by_id = {paper.paper_id: paper for paper in papers}
        claims = tuple(
            AtomicClaim(
                text=card.atomic_finding,
                evidence_ids=(card.evidence_id,),
                section="Evidence synthesis",
            )
            for card in evidence
        )
        if claims:
            body = "\n".join(
                f"- {claim.text} [{index}]" for index, claim in enumerate(claims, start=1)
            )
            summary = (
                f"The evidence-first workflow found {len(evidence)} traceable findings across "
                f"{len({card.paper_id for card in evidence})} sources."
            )
        else:
            body = "No traceable evidence was retrieved within the configured budget."
            summary = "Evidence is insufficient; the report requires human review."
        references = tuple(
            _reference(index, paper_by_id[card.paper_id])
            for index, card in enumerate(evidence, start=1)
            if card.paper_id in paper_by_id
        )
        section = ReportSection(
            heading="Evidence synthesis",
            body=body,
            claim_ids=tuple(claim.claim_id for claim in claims),
        )
        title = f"Research brief: {question}"
        markdown = _render_markdown(title, summary, section, references)
        return (
            ResearchReport(
                title=title,
                executive_summary=summary,
                sections=(section,),
                references=references,
                markdown=markdown,
            ),
            claims,
        )

    async def verify(
        self,
        claim: AtomicClaim,
        evidence: tuple[EvidenceCard, ...],
        passages: tuple[Passage, ...],
    ) -> VerificationResult:
        evidence_by_id = {card.evidence_id: card for card in evidence}
        passage_by_id = {passage.passage_id: passage for passage in passages}
        checked = tuple(
            passage_id
            for evidence_id in claim.evidence_ids
            if (card := evidence_by_id.get(evidence_id))
            for passage_id in card.passage_ids
            if passage_id in passage_by_id
        )
        source_text = " ".join(passage_by_id[item].text for item in checked)
        claim_tokens = tokenize(claim.text)
        overlap = len(claim_tokens & tokenize(source_text)) / max(1, len(claim_tokens))
        exact = claim.text.lower() in source_text.lower()
        if exact or overlap >= 0.8:
            status, confidence = VerificationStatus.SUPPORTED, max(0.9, overlap)
        elif overlap >= 0.5:
            status, confidence = VerificationStatus.PARTIAL, overlap
        else:
            status, confidence = VerificationStatus.UNSUPPORTED, 1 - overlap
        return VerificationResult(
            claim_id=claim.claim_id,
            status=status,
            confidence=min(1.0, confidence),
            rationale=f"Lexical evidence coverage={overlap:.2f}; exact_match={exact}.",
            checked_passage_ids=checked,
        )


def _reference(index: int, paper: Paper) -> str:
    authors = ", ".join(paper.authors) or "Unknown author"
    identifier = f"https://doi.org/{paper.doi}" if paper.doi else str(paper.url or "")
    return f"[{index}] {authors}. {paper.title}. {paper.year or 'n.d.'}. {identifier}".strip()


def _render_markdown(
    title: str, summary: str, section: ReportSection, references: tuple[str, ...]
) -> str:
    references_text = "\n".join(references) or "No references available."
    return (
        f"# {title}\n\n"
        f"## Executive summary\n\n{summary}\n\n"
        f"## {section.heading}\n\n{section.body}\n\n"
        f"## References\n\n{references_text}\n"
    )
