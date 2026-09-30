"""OpenAI provider — alternative commentator LLM (gpt-4o-mini etc)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from voice_copilot.core.secrets import get_secret
from voice_copilot.providers.llm.base import LLMMessage, LLMProvider
from voice_copilot.providers.registry import register

log = logging.getLogger(__name__)

#: Per request, and per gap between streamed chunks.
_TIMEOUT_S = 60.0

#: Model families that reason before answering. The Chat Completions API
#: refuses `max_tokens` and any non-default `temperature` for them.
_REASONING_FAMILIES = ("o1", "o3", "o4", "gpt-5")
#: Reasoning tokens count against the completion budget: a two-sentence
#: narration needs room for the thinking in front of it.
_REASONING_BUDGET = 2048


def is_reasoning_model(model: str) -> bool:
    """o-series and GPT-5 models, also behind a router prefix (`openai/gpt-5-mini`)."""
    name = model.rsplit("/", 1)[-1].lower()
    # gpt-5-chat-latest is the non-reasoning chat variant.
    return name.startswith(_REASONING_FAMILIES) and "-chat" not in name


def _default_effort(model: str) -> str:
    # GPT-5 answers fastest with "minimal"; the o-series starts at "low".
    return "minimal" if model.rsplit("/", 1)[-1].lower().startswith("gpt-5") else "low"


def _param_rejected(error: Exception) -> bool:
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "unsupported_parameter",
        )
    )


@register("llm", "openai")
class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key or get_secret("OPENAI_API_KEY")
        self._base_url = base_url
        self._client: Any = None
        self._reasoning = is_reasoning_model(model)
        self._reasoning_effort = reasoning_effort or _default_effort(model)
        self._send_effort = True

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            # The SDK default is 10 minutes; one stalled narration would hold
            # every line after it for that long.
            self._client = AsyncOpenAI(
                api_key=self._api_key, base_url=self._base_url, timeout=_TIMEOUT_S
            )
        return self._client

    async def stream_chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.4,
    ) -> AsyncIterator[str]:
        client = self._get_client()
        chat_msgs: list[dict[str, str]] = []
        if system:
            chat_msgs.append({"role": "system", "content": system})
        for m in messages:
            chat_msgs.append({"role": m.role, "content": m.content})

        try:
            stream = await client.chat.completions.create(
                model=self._model,
                messages=chat_msgs,
                stream=True,
                **self._sampling(max_tokens, temperature),
            )
        except Exception as e:
            # A model we don't know by name (a new family, a router alias)
            # tells us which style it wants; switch once and retry.
            if self._reasoning and self._send_effort and "reasoning_effort" in str(e).lower():
                self._send_effort = False  # older o-series models take no effort setting
            elif _param_rejected(e):
                self._reasoning = not self._reasoning
            else:
                raise
            log.info(
                "openai: %s refused the request parameters; retrying as a %s model",
                self._model,
                "reasoning" if self._reasoning else "non-reasoning",
            )
            stream = await client.chat.completions.create(
                model=self._model,
                messages=chat_msgs,
                stream=True,
                **self._sampling(max_tokens, temperature),
            )
        chunks = 0
        yielded = 0
        async for chunk in stream:
            chunks += 1
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yielded += 1
                yield delta.content
        if yielded == 0 and chunks > 0:
            log.warning(
                "openai stream: %d chunks, 0 content deltas — model may be a "
                "reasoning model putting output in `reasoning` field; try a "
                "non-thinking model",
                chunks,
            )

    def _sampling(self, max_tokens: int, temperature: float) -> dict[str, Any]:
        if self._reasoning:
            params: dict[str, Any] = {"max_completion_tokens": max(max_tokens, _REASONING_BUDGET)}
            if self._send_effort:
                params["reasoning_effort"] = self._reasoning_effort
            return params
        return {"max_tokens": max_tokens, "temperature": temperature}
