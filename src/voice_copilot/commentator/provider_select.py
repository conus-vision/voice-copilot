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


def resolve_for_launch(
    cmt: CommentatorConfig, *, cli: str | None, binary: str | None
) -> tuple[CommentatorConfig, str]:
    """(effective commentator config, panel status) for a launched CLI.

    Returns a copy with the effective provider and supervisor: the saved
    config keeps the user's API choice, and the runtime `auto` provider
    (which carries an absolute binary path) never round-trips into it.
    """
    effective = resolve_commentator_provider(cmt, cli=cli, binary=binary)
    commentator_cfg = cmt.model_copy(deep=True)
    commentator_cfg.provider = effective
    commentator_cfg.supervisor = resolve_supervisor(cmt, cli=cli)
    status = commentator_status_text(effective, cli)
    sup_status = supervisor_status_text(commentator_cfg.supervisor)
    if sup_status:
        status = f"{status}  •  {sup_status}"
    return commentator_cfg, status


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
