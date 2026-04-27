"""String-keyed registry for provider classes.

Providers self-register at import time with `@register("kind", "name")`.
Callers resolve them with `build(kind, ProviderConfig)`.

The registry is intentionally dumb — no auto-discovery, no entry-points.
We import a single top-level module per kind (`providers.llm`, etc.) which
in turn imports concrete implementations. Optional backends live behind
`try: import …` guards.
"""

from __future__ import annotations

from typing import Any, Literal

ProviderKind = Literal["llm", "tts", "stt"]

_registry: dict[ProviderKind, dict[str, type[Any]]] = {"llm": {}, "tts": {}, "stt": {}}


def register(kind: ProviderKind, name: str):  # type: ignore[no-untyped-def]
    def deco(cls: type[Any]) -> type[Any]:
        _registry[kind][name] = cls
        return cls

    return deco


def get(kind: ProviderKind, name: str) -> type[Any]:
    try:
        return _registry[kind][name]
    except KeyError as e:
        known = ", ".join(sorted(_registry[kind])) or "<none registered>"
        raise KeyError(f"{kind} provider {name!r} not registered. Known: {known}") from e


def build(kind: ProviderKind, name: str, options: dict[str, Any] | None = None) -> Any:
    cls = get(kind, name)
    return cls(**(options or {}))


def names(kind: ProviderKind) -> list[str]:
    return sorted(_registry[kind])
