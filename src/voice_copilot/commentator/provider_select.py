"""Resolve the effective commentator provider for a `vc` launch, honoring the
global auto/api mode and optional per-CLI overrides, and describe it for the
panel status indicator.
"""

from __future__ import annotations

from voice_copilot.commentator.model_catalog import model_tiers
from voice_copilot.core.config import CommentatorConfig, ProviderConfig, SupervisorConfig


def resolve_commentator_provider(
    cmt: CommentatorConfig, *, cli: str | None, binary: str | None
) -> ProviderConfig:
    override = cmt.per_cli.get(cli) if cli else None
    effective = "current" if cmt.mode == "auto" else "api"
    model = None
    if override is not None:
        if override.mode != "default":
            effective = override.mode
        model = override.model
    if not model and cmt.auto_tier_models:
        tiers = model_tiers(cli)
        if tiers is not None:
            model = tiers.weakest

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


def resolve_supervisor(cmt: CommentatorConfig, *, cli: str | None) -> SupervisorConfig:
    """Effective supervisor settings for this launch: global, then the
    per-CLI override, then the auto-tier pick for a missing model.
    """
    sup = cmt.supervisor.model_copy()
    override = cmt.per_cli.get(cli) if cli else None
    if override is not None:
        if override.supervisor_mode != "default":
            sup.mode = override.supervisor_mode
        if override.supervisor_model:
            sup.model = override.supervisor_model
    if not sup.model and cmt.auto_tier_models:
        tiers = model_tiers(cli)
        if tiers is not None:
            sup.model = tiers.strongest
    return sup


def supervisor_status_text(sup: SupervisorConfig) -> str | None:
    if sup.mode == "off":
        return None
    label = "Supervisor+" if sup.mode == "guard" else "Supervisor"
    model = f" ({sup.model})" if sup.model else ""
    return f"{label}{model} on"


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
