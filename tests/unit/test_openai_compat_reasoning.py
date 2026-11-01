"""A thinking model on Ollama answers only if it's asked not to think."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from voice_copilot.providers.llm.base import LLMMessage
from voice_copilot.providers.llm.openai_compat import OpenAICompatProvider


class _FakeCompletions:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.kwargs = kwargs

        async def stream() -> AsyncIterator[Any]:
            delta = SimpleNamespace(content="hello")
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

        return stream()


async def _request(provider: OpenAICompatProvider) -> dict[str, Any]:
    completions = _FakeCompletions()
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    chunks = [
        chunk async for chunk in provider.stream_chat([LLMMessage(role="user", content="hi")])
    ]
    assert chunks == ["hello"]
    return completions.kwargs


@pytest.mark.asyncio
async def test_thinking_is_off_by_default() -> None:
    kwargs = await _request(OpenAICompatProvider(model="gemma4:12b", api_key="local"))
    assert kwargs["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_empty_setting_lets_the_model_think() -> None:
    provider = OpenAICompatProvider(model="gemma4:12b", api_key="local", reasoning_effort="")
    assert "reasoning_effort" not in await _request(provider)
