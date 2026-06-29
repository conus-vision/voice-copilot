from voice_copilot.cli import _apply_commentator_resolution
from voice_copilot.core.config import Config
from voice_copilot.proxy.cli_shims import ResolvedCli


def test_known_cli_sets_auto_provider_and_status() -> None:
    cfg = Config()  # commentator.mode defaults to auto
    resolved = ResolvedCli(
        profile_id="claude",
        label="Claude Code",
        resolved_binary="/usr/bin/claude",
        env_overrides={"ANTHROPIC_BASE_URL": "http://x"},
        working_directory=None,
    )
    status = _apply_commentator_resolution(cfg, resolved)
    assert cfg.commentator.provider.name == "auto"
    assert cfg.commentator.provider.options["cli"] == "claude"
    assert "claude" in status.lower()


def test_unknown_cli_leaves_auto_without_cli() -> None:
    cfg = Config()
    status = _apply_commentator_resolution(cfg, None)
    # mode=auto + no cli → auto provider with no cli (will fail loud at narrate)
    assert cfg.commentator.provider.name == "auto"
    assert "cli" not in cfg.commentator.provider.options
    assert "Commentator" in status
