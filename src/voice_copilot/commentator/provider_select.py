"""Resolve the effective commentator provider for a `vc` launch, honoring the
global auto/api mode and optional per-CLI overrides, and describe it for the
panel status indicator.
"""

from __future__ import annotations

from voice_copilot.core.config import CommentatorConfig, ProviderConfig


def resolve_commentator_provider(
    cmt: CommentatorConfig, *, cli: str | None, binary: str | None
) -> ProviderConfig:
    override = cmt.per_cli.get(cli) if cli else None
    if override is not None:
        effective = override.mode  # "current" | "api"
        model = override.model
    else:
        effective = "current" if cmt.mode == "auto" else "api"
        model = None

    if effective == "api":
        return cmt.provider

    options: dict[str, str | int | float | bool] = {}
    if cli:
        options["cli"] = cli
    if binary:
        options["binary"] = binary
    if model:
        options["model"] = model
    return ProviderConfig(name="auto", options=options)


def commentator_status_text(provider: ProviderConfig, cli: str | None) -> str:
    if provider.name == "auto":
        target = provider.options.get("cli") or cli
        if not target:
            # auto with no launched CLI (not run via vc, or unsupported CLI)
            return (
                "Commentator: auto needs a vc-launched supported CLI — "
                "pick a provider in the Commentator tab."
            )
        model = provider.options.get("model")
        suffix = f", {model}" if model else ""
        return f"Commentator: {target} (current CLI{suffix}) — change in the Commentator tab"
    model = provider.options.get("model")
    model_part = f" {model}" if model else ""
    return f"Commentator: {provider.name}{model_part} (API) — change in the Commentator tab"
