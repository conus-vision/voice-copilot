"""Common LLM interface used by the commentator pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant"]


class LLMMessage(BaseModel):
    role: Role
    content: str


class LLMProvider(ABC):
    """Abstract streaming chat completion."""

    name: str = "unknown"

    @abstractmethod
    async def stream_chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.4,
    ) -> AsyncIterator[str]:
        """Yield response text in deltas as the model streams."""
        raise NotImplementedError
