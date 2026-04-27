"""Pluggable LLM / TTS / STT providers and a small registry."""

from voice_copilot.providers.registry import ProviderKind, build, get, register

__all__ = ["ProviderKind", "build", "get", "register"]
