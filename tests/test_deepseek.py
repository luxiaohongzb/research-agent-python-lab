import json
from typing import Any

import httpx
import pytest
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from research_agent.config import Settings
from research_agent.domain import ResearchRequest
from research_agent.llm_reasoner import LangChainStructuredReasoner
from research_agent.model_stream import bind_model_stream
from research_agent.workflow import build_default_workflow


def test_deepseek_api_key_supports_official_environment_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")

    settings = Settings(_env_file=None)

    assert settings.deepseek_api_key is not None
    assert settings.deepseek_api_key.get_secret_value() == "sk-deepseek-test"


def test_deepseek_mode_builds_chat_completions_json_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langchain_openai

    captured: dict[str, Any] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)
    workflow = build_default_workflow(
        Settings(
            reasoner_mode="deepseek",
            deepseek_api_key=SecretStr("sk-deepseek-test"),
            deepseek_model="deepseek-v4-flash",
            deepseek_base_url="https://api.deepseek.com",
        )
    )

    assert captured["model"] == "deepseek-v4-flash"
    assert captured["base_url"] == "https://api.deepseek.com"
    assert captured["use_responses_api"] is False
    assert captured["streaming"] is True
    assert captured["stream_usage"] is True
    assert captured["api_key"].get_secret_value() == "sk-deepseek-test"
    primary = workflow._reasoner._primary  # type: ignore[attr-defined]
    assert primary._provider == "deepseek"
    assert primary._structured_output_method == "json_mode"


def test_deepseek_mode_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        build_default_workflow(
            Settings(
                reasoner_mode="deepseek",
                deepseek_api_key=None,
            )
        )


@pytest.mark.asyncio
async def test_deepseek_adapter_uses_chat_completions_and_json_output() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        payload = json.loads(request.content)
        requests.append(payload)
        content = {
            "complexity": "SIMPLE",
            "objective": "Verify scientific claims",
            "sub_questions": ["How are scientific claims verified?"],
            "queries": [
                {
                    "sub_question": "How are scientific claims verified?",
                    "query": "scientific claim verification evidence",
                    "purpose": "Find validation methods",
                }
            ],
            "inclusion_criteria": ["Traceable evidence"],
            "exclusion_criteria": ["Unsupported claims"],
        }
        serialized = json.dumps(content)
        chunks = [serialized[:80], serialized[80:]]
        events = []
        for index, delta in enumerate(chunks):
            events.append(
                "data: "
                + json.dumps(
                    {
                        "id": "deepseek-test",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "deepseek-v4-pro",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant" if index == 0 else None,
                                    "content": delta,
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            )
        events.extend(
            [
                "data: "
                + json.dumps(
                    {
                        "id": "deepseek-test",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "deepseek-v4-pro",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                ),
                "data: "
                + json.dumps(
                    {
                        "id": "deepseek-test",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "deepseek-v4-pro",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 20,
                            "completion_tokens": 10,
                            "total_tokens": 30,
                        },
                    }
                ),
                "data: [DONE]",
            ]
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=("\n\n".join(events) + "\n\n").encode(),
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        model = ChatOpenAI(
            model="deepseek-v4-pro",
            api_key=SecretStr("sk-deepseek-test"),
            base_url="https://api.deepseek.com",
            http_async_client=async_client,
            use_responses_api=False,
            streaming=True,
            stream_usage=True,
            max_retries=0,
        )
        reasoner = LangChainStructuredReasoner(
            model,
            provider="deepseek",
            model_name="deepseek-v4-pro",
            structured_output_method="json_mode",
            stream_json_mode=True,
        )
        stream_events: list[tuple[str, dict[str, object]]] = []

        async def capture(stage: str, details: dict[str, object]) -> None:
            stream_events.append((stage, details))

        with bind_model_stream(capture):
            result = await reasoner.plan(
                ResearchRequest(question="How are scientific claims verified?")
            )
    finally:
        await async_client.aclose()

    assert result.value.objective == "Verify scientific claims"
    assert requests[0]["model"] == "deepseek-v4-pro"
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["stream"] is True
    assert [details["phase"] for _, details in stream_events][0] == "started"
    assert any(details["phase"] == "delta" for _, details in stream_events)
    assert stream_events[-1][1]["phase"] == "completed"
    assert stream_events[-1][1]["validated"] is True
