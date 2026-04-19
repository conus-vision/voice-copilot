"""LLM providers — import side-effects register implementations."""

from voice_copilot.providers.llm.base import LLMMessage, LLMProvider

# Side-effect imports — each module registers itself.
from voice_copilot.providers.llm import anthropic as _anthropic  # noqa: F401
from voice_copilot.providers.llm import openai as _openai  # noqa: F401
from voice_copilot.providers.llm import openai_compat as _openai_compat  # noqa: F401

__all__ = ["LLMMessage", "LLMProvider"]
