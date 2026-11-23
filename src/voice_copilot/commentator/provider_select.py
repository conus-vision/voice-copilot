"""Resolve the effective commentator provider for a `vc` launch, honoring the
global auto/api mode and optional per-CLI overrides, and describe it for the
panel status indicator.
"""

from __future__ import annotations

import shutil

from voice_copilot.commentator.model_catalog import model_tiers
from voice_copilot.core.config import (
    CommentatorConfig,
    ProviderConfig,
    SupervisorConfig,
    work_mode,
)


def narrator_cli_for(cmt: CommentatorConfig, cli: str | None) -> str | None:
    """Another CLI the narrator runs through while `cli` is watched, or None."""
    override = cmt.per_cli.get(cli) if cli else None
    chosen = (override.narrator_cli if override is not None else None) or cmt.narrator_cli
    return chosen if chosen and chosen != cli else None


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
    other = narrator_cli_for(cmt, cli)
    if other is not None:
        # Asked for by name, so it wins over `api`. Not found is the provider's error to
        # report when it runs, never a quiet switch back to the watched CLI.
        effective, cli, binary = "current", other, shutil.which(other)
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
        if override.supervisor_cli:
            sup.cli = override.supervisor_cli
    if sup.cli == cli:
        sup.cli = None  # the watched CLI is the default anyway
    if not sup.model and cmt.auto_tier_models:
        tiers = model_tiers(sup.cli or cli)
        if tiers is not None:
            sup.model = tiers.strongest
    return sup


def resolve_narrator(cmt: CommentatorConfig, sup: SupervisorConfig, *, cli: str | None) -> bool:
    """Whether the narrator speaks. Never off with no supervisor: that would be silence."""
    override = cmt.per_cli.get(cli) if cli else None
    on = cmt.narrator
    if override is not None and override.narrator != "default":
        on = override.narrator == "on"
    return on or sup.mode == "off"


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
    commentator_cfg.narrator = resolve_narrator(cmt, commentator_cfg.supervisor, cli=cli)
    status = commentator_status_text(effective, cli) if commentator_cfg.narrator else None
    sup_status = supervisor_status_text(commentator_cfg.supervisor)
    status = "  •  ".join(part for part in (status, sup_status) if part) or "Narrator off"
    return commentator_cfg, status


#: How the panel and `vc mode` name the four ways of working.
MODE_LABELS = {
    "narrator": "Narrator",
    "supervisor": "Supervisor",
    "plus": "Supervisor+",
    "plus-narrator": "Supervisor+ with narrator",
}


def supervisor_status_text(sup: SupervisorConfig) -> str | None:
    if sup.mode == "off":
        return None
    label = MODE_LABELS[work_mode(True, sup.mode)] if sup.mode == "watch" else "Supervisor+"
    via = f"{sup.cli}, " if sup.cli else ""
    model = f" ({via}{sup.model})" if sup.model else (f" ({sup.cli})" if sup.cli else "")
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
        whose = "current CLI" if not cli or target == cli else "its own login"
        return f"Commentator: {target} ({whose}{suffix}) — change in the Commentator tab"
    model = provider.options.get("model")
    model_part = f" {model}" if model else ""
    return f"Commentator: {provider.name}{model_part} (API) — change in the Commentator tab"
