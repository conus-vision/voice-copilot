"""OpenAI reasoning models get the parameters they accept.

o-series and GPT-5 models refuse `max_tokens` and a non-default
`temperature`; before, every narration on them failed with a 400.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from voice_copilot.providers.llm.base import LLMMessage
from voice_copilot.providers.llm.openai import OpenAIProvider, is_reasoning_model


class _Chunk:
    def __init__(self, text: str) -> None:
        delta = type("Delta", (), {"content": text})()
        self.choices = [type("Choice", (), {"delta": delta})()]


class _FakeCompletions:
    def __init__(self, refuse: list[str]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._refuse = list(refuse)

    async def create(self, **kwargs: Any) -> AsyncIterator[_Chunk]:
        self.calls.append(kwargs)
        for param in self._refuse:
            if param in kwargs:
                self._refuse.remove(param)
                raise RuntimeError(f"Unsupported parameter: '{param}' is not supported")

        async def gen() -> AsyncIterator[_Chunk]:
            yield _Chunk("Reading the parser.")

        return gen()


def _provider(model: str, refuse: list[str] | None = None) -> tuple[OpenAIProvider, Any]:
    provider = OpenAIProvider(model=model, api_key="sk-test")
    completions = _FakeCompletions(refuse or [])
    provider._client = type(
        "Client", (), {"chat": type("Chat", (), {"completions": completions})()}
    )()
    return provider, completions


async def _say(provider: OpenAIProvider) -> str:
    out = []
    async for piece in provider.stream_chat(
        [LLMMessage(role="user", content="events")], system="narrate", max_tokens=160
    ):
        out.append(piece)
    return "".join(out)


@pytest.mark.parametrize(
    ("model", "reasoning"),
    [
        ("gpt-5-mini", True),
        ("o4-mini", True),
        ("openai/gpt-5", True),
        ("gpt-5-chat-latest", False),
        ("gpt-4o-mini", False),
        ("gpt-4.1", False),
    ],
)
def test_reasoning_models_are_recognized(model: str, reasoning: bool) -> None:
    assert is_reasoning_model(model) is reasoning


async def test_reasoning_model_gets_completion_budget_and_effort() -> None:
    provider, completions = _provider("gpt-5-mini")
    assert await _say(provider) == "Reading the parser."
    call = completions.calls[0]
    assert "max_tokens" not in call and "temperature" not in call
    assert call["max_completion_tokens"] >= 1024
    assert call["reasoning_effort"] == "minimal"


async def test_chat_model_keeps_classic_parameters() -> None:
    provider, completions = _provider("gpt-4o-mini")
    await _say(provider)
    assert completions.calls[0]["max_tokens"] == 160
    assert completions.calls[0]["temperature"] == 0.4


async def test_an_unknown_model_that_refuses_max_tokens_is_retried_as_reasoning() -> None:
    provider, completions = _provider("acme-thinker-2", refuse=["max_tokens"])
    assert await _say(provider) == "Reading the parser."
    assert "max_completion_tokens" in completions.calls[1]
    # Remembered for the next line.
    await _say(provider)
    assert "max_completion_tokens" in completions.calls[2]


async def test_an_old_o_model_without_effort_support() -> None:
    provider, completions = _provider("o1-mini", refuse=["reasoning_effort"])
    assert await _say(provider) == "Reading the parser."
    assert "reasoning_effort" not in completions.calls[1]
    assert "max_completion_tokens" in completions.calls[1]
