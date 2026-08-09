import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from research_agent.domain import ResearchRequest, RunStatus, VerificationStatus
from research_agent.llm_reasoner import (
    EvidenceOutput,
    FallbackResearchReasoner,
    LangChainStructuredReasoner,
    PlanOutput,
    SynthesisOutput,
    VerificationOutput,
)
from research_agent.prompts import PROMPT_VERSION
from research_agent.providers import CompositePaperProvider, OfflinePaperProvider
from research_agent.workflow import ResearchWorkflow


class ScriptedRunnable:
    def __init__(self, schema: type[Any], *, fail: bool = False) -> None:
        self._schema = schema
        self._fail = fail

    async def ainvoke(self, messages: list[Any]) -> dict[str, Any]:
        raw = AIMessage(
            content="",
            response_metadata={"model_name": "fake-structured-model"},
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
        if self._fail:
            return {"raw": raw, "parsed": None, "parsing_error": ValueError("invalid schema")}
        payload = json.loads(str(messages[-1].content))["input"]
        if self._schema is PlanOutput:
            data = {
                "complexity": "SIMPLE",
                "objective": payload["question"],
                "sub_questions": [payload["question"]],
                "queries": [
                    {
                        "sub_question": payload["question"],
                        "query": "agentic RAG claim verification",
                        "purpose": "core evidence",
                    }
                ],
                "inclusion_criteria": ["Relevant and traceable"],
                "exclusion_criteria": ["Missing evidence text"],
            }
        elif self._schema is EvidenceOutput:
            sentence = payload["passage"]["text"].split(".", maxsplit=1)[0] + "."
            data = {
                "relevant": True,
                "atomic_finding": sentence,
                "evidence_type": "DIRECT",
                "limitations": ["Abstract-only fixture"],
                "source_quality": "MEDIUM",
                "confidence": 0.9,
            }
        elif self._schema is SynthesisOutput:
            evidence = payload["evidence"]
            data = {
                "title": "Structured research brief",
                "executive_summary": "The supplied evidence supports a traceable synthesis.",
                "sections": [
                    {
                        "heading": "Evidence synthesis",
                        "claims": [
                            {
                                "text": item["atomic_finding"],
                                "evidence_ids": [item["evidence_id"]],
                            }
                            for item in evidence
                        ],
                    }
                ],
            }
        elif self._schema is VerificationOutput:
            data = {
                "status": "SUPPORTED",
                "confidence": 0.95,
                "rationale": "The bound passage contains the complete claim.",
            }
        else:
            raise AssertionError(f"unexpected schema: {self._schema}")
        return {"raw": raw, "parsed": self._schema.model_validate(data), "parsing_error": None}


class ScriptedModel:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def with_structured_output(self, schema: type[Any], **_: Any) -> ScriptedRunnable:
        return ScriptedRunnable(schema, fail=self._fail)


@pytest.mark.asyncio
async def test_structured_reasoner_runs_end_to_end_and_records_model_calls() -> None:
    reasoner = LangChainStructuredReasoner(
        ScriptedModel(),
        provider="fake",
        model_name="fake-structured-model",
        input_cost_per_million_usd=1,
        output_cost_per_million_usd=2,
    )
    workflow = ResearchWorkflow(
        provider=CompositePaperProvider((OfflinePaperProvider(),)),
        reasoner=reasoner,
    )

    result = await workflow.run(
        ResearchRequest(question="How should a research agent verify claims?")
    )

    assert result.status is RunStatus.COMPLETED
    assert result.model_invocations
    assert all(item.success for item in result.model_invocations)
    assert all(item.prompt_version == PROMPT_VERSION for item in result.model_invocations)
    assert all(item.input_tokens == 10 for item in result.model_invocations)
    assert all(item.estimated_cost_usd == 0.00002 for item in result.model_invocations)
    assert all(item.status is VerificationStatus.SUPPORTED for item in result.verifications)


@pytest.mark.asyncio
async def test_structured_failure_is_recorded_before_deterministic_fallback() -> None:
    primary = LangChainStructuredReasoner(
        ScriptedModel(fail=True),
        provider="fake",
        model_name="fake-structured-model",
    )
    reasoner = FallbackResearchReasoner(primary)

    result = await reasoner.plan(ResearchRequest(question="agentic RAG claim verification"))

    assert result.value.search_tasks
    assert len(result.model_invocations) == 2
    assert all(not item.success for item in result.model_invocations)
    assert result.warnings == ("plan: structured model failed; deterministic fallback used.",)
