from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

ModelStreamCallback = Callable[[str, dict[str, object]], Awaitable[None]]

_MODEL_STREAM_CALLBACK: ContextVar[ModelStreamCallback | None] = ContextVar(
    "research_agent_model_stream_callback",
    default=None,
)


@contextmanager
def bind_model_stream(callback: ModelStreamCallback | None) -> Iterator[None]:
    """Bind a run-local model stream without sharing callbacks across concurrent runs."""

    token = _MODEL_STREAM_CALLBACK.set(callback)
    try:
        yield
    finally:
        _MODEL_STREAM_CALLBACK.reset(token)


async def emit_model_stream(stage: str, details: dict[str, object]) -> None:
    callback = _MODEL_STREAM_CALLBACK.get()
    if callback is not None:
        await callback(stage, details)
