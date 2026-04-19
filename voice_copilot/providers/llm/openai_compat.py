"""OpenAI-compatible local endpoints (Ollama, LM Studio, llama.cpp server).

Thin subclass of the OpenAI provider pointing at a local base_url.
"""

from __future__ import annotations

import os

from voice_copilot.core.secrets import get_secret
from voice_copilot.providers.llm.openai import OpenAIProvider
from voice_copilot.providers.registry import register


@register("llm", "openai-compat")
class OpenAICompatProvider(OpenAIProvider):
    name = "openai-compat"

    def __init__(
        self,
        model: str = "llama3.1",
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        base = base_url or os.getenv("OPENAI_COMPAT_BASE_URL") or "http://127.0.0.1:11434/v1"
        key = api_key or get_secret("OPENAI_COMPAT_API_KEY") or "local"
        super().__init__(model=model, api_key=key, base_url=base)
