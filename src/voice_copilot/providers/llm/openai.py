"""OpenAI provider — alternative commentator LLM (gpt-4o-mini etc)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from voice_copilot.core.secrets import get_secret
from voice_copilot.providers.llm.base import LLMMessage, LLMProvider
from voice_copilot.providers.registry import register

log = logging.getLogger(__name__)


@register("llm", "openai")
class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key or get_secret("OPENAI_API_KEY")
        self._base_url = base_url
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)
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

        stream = await client.chat.completions.create(
            model=self._model,
            messages=chat_msgs,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
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
