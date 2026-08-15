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
    def __init__(
        self,
        schema: type[Any],
        *,
        fail: bool = False,
        input_tokens: int = 10,
        output_tokens: int = 5,
    ) -> None:
        self._schema = schema
        self._fail = fail
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    async def ainvoke(self, messages: list[Any]) -> dict[str, Any]:
        raw = AIMessage(
            content="",
            response_metadata={"model_name": "fake-structured-model"},
            usage_metadata={
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "total_tokens": self._input_tokens + self._output_tokens,
            },
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
    def __init__(
        self,
        *,
        fail: bool = False,
        input_tokens: int = 10,
        output_tokens: int = 5,
    ) -> None:
        self._fail = fail
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.structured_options: list[dict[str, Any]] = []

    def with_structured_output(self, schema: type[Any], **options: Any) -> ScriptedRunnable:
        self.structured_options.append(options)
        return ScriptedRunnable(
            schema,
            fail=self._fail,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )


@pytest.mark.asyncio
async def test_json_mode_uses_deepseek_compatible_structured_output() -> None:
    model = ScriptedModel()
    reasoner = LangChainStructuredReasoner(
        model,
        provider="deepseek",
        model_name="deepseek-v4-pro",
        structured_output_method="json_mode",
    )

    result = await reasoner.plan(
        ResearchRequest(question="How should scientific claims be verified?")
    )

    assert result.value.search_tasks
    assert model.structured_options == [{"method": "json_mode", "include_raw": True}]
    assert result.model_invocations[0].provider == "deepseek"


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
    assert result.budget.used_total_tokens == sum(
        (item.input_tokens or 0) + (item.output_tokens or 0) for item in result.model_invocations
    )
    assert result.budget.estimated_cost_usd == pytest.approx(
        sum(item.estimated_cost_usd or 0 for item in result.model_invocations)
    )
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


@pytest.mark.asyncio
async def test_model_budget_switches_remaining_stages_to_deterministic_baseline() -> None:
    reasoner = LangChainStructuredReasoner(
        ScriptedModel(input_tokens=400, output_tokens=200),
        provider="fake",
        model_name="fake-structured-model",
    )
    workflow = ResearchWorkflow(
        provider=CompositePaperProvider((OfflinePaperProvider(),)),
        reasoner=reasoner,
    )

    result = await workflow.run(
        ResearchRequest(
            question="How should a research agent verify claims?",
            max_total_tokens=1_000,
        )
    )

    assert result.status is RunStatus.NEEDS_REVIEW
    assert result.budget.used_total_tokens == 1_200
    assert len(result.model_invocations) == 2
    assert "tokens" in result.budget.exhausted_limits
    assert any("Run limits reached" in warning for warning in result.warnings)
