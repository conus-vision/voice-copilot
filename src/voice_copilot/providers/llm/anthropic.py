"""Anthropic (Claude) provider — default commentator LLM."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from voice_copilot.core.secrets import get_secret
from voice_copilot.providers.llm.base import LLMMessage, LLMProvider
from voice_copilot.providers.registry import register


@register("llm", "anthropic")
class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self, model: str = "claude-haiku-4-5-20251001", api_key: str | None = None
    ) -> None:
        self._model = model
        self._api_key = api_key or get_secret("ANTHROPIC_API_KEY")
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self._api_key)
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
        # Anthropic API wants user/assistant-only messages; system is a top-level arg.
        payload = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        system_text = system or next((m.content for m in messages if m.role == "system"), None)

        async with client.messages.stream(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_text or "",
            messages=payload,
        ) as stream:
            async for delta in stream.text_stream:
                if delta:
                    yield delta
