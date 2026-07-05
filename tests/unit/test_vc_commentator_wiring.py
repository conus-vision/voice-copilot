from voice_copilot.cli import _apply_commentator_resolution, _launch_notice
from voice_copilot.core.config import Config
from voice_copilot.proxy.cli_shims import ResolvedCli


def _resolved(profile_id: str, provider: str = "anthropic") -> ResolvedCli:
    return ResolvedCli(
        profile_id=profile_id,
        label=profile_id,
        resolved_binary=f"/usr/bin/{profile_id}",
        env_overrides={},
        working_directory=None,
        provider=provider,
    )


def test_launch_notice_appends_remote_control_note_for_claude() -> None:
    notice = _launch_notice(_resolved("claude"), "Commentator: auto (claude)")
    assert "Commentator: auto (claude)" in notice
    assert "Remote Control" in notice


def test_launch_notice_plain_for_non_claude() -> None:
    notice = _launch_notice(_resolved("codex", provider="openai"), "Commentator: auto (codex)")
    assert notice == "Commentator: auto (codex)"


def test_launch_notice_flags_unnarrated_provider() -> None:
    # gemini is a blind passthrough in the proxy — the banner must say so.
    notice = _launch_notice(_resolved("gemini", provider="gemini"), "Commentator: auto (gemini)")
    assert "not narrated yet" in notice


def test_launch_notice_plain_when_unresolved() -> None:
    assert _launch_notice(None, "Commentator: off") == "Commentator: off"


def test_known_cli_returns_auto_provider_and_status() -> None:
    cfg = Config()  # commentator.mode defaults to auto
    resolved = ResolvedCli(
        profile_id="claude",
        label="Claude Code",
        resolved_binary="/usr/bin/claude",
        env_overrides={"ANTHROPIC_BASE_URL": "http://x"},
        working_directory=None,
        provider="anthropic",
    )
    commentator_cfg, status = _apply_commentator_resolution(cfg, resolved)
    assert commentator_cfg.provider.name == "auto"
    assert commentator_cfg.provider.options["cli"] == "claude"
    assert "claude" in status.lower()


def test_known_cli_does_not_mutate_the_shared_config() -> None:
    # Regression: the runtime auto provider (absolute binary path) must NOT
    # leak into the shared cfg the web server exposes via /api/config.
    cfg = Config()
    resolved = ResolvedCli(
        profile_id="claude",
        label="Claude Code",
        resolved_binary="/usr/bin/claude",
        env_overrides={},
        working_directory=None,
        provider="anthropic",
    )
    _apply_commentator_resolution(cfg, resolved)
    assert cfg.commentator.provider.name != "auto"
    assert "binary" not in cfg.commentator.provider.options


def test_unknown_cli_returns_auto_without_cli() -> None:
    cfg = Config()
    commentator_cfg, status = _apply_commentator_resolution(cfg, None)
    # mode=auto + no cli → auto provider with no cli (will fail loud at narrate)
    assert commentator_cfg.provider.name == "auto"
    assert "cli" not in commentator_cfg.provider.options
    assert "Commentator" in status
    assert cfg.commentator.provider.name != "auto"  # shared cfg untouched
