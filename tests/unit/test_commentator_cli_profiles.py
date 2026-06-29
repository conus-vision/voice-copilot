import pytest

from voice_copilot.commentator.cli_profiles import (
    NARRATION_PROFILES,
    build_narration_command,
)


def test_known_clis_have_profiles() -> None:
    assert set(NARRATION_PROFILES) >= {"claude", "codex", "opencode", "gemini", "copilot"}


def test_claude_command_uses_cheap_model_and_no_tools_arg_mode() -> None:
    argv, stdin_text = build_narration_command("claude", "/usr/bin/claude", "NARRATE THIS")
    assert argv[0] == "/usr/bin/claude"
    assert "--model" in argv
    assert "--allowedTools" in argv
    # arg mode → prompt is the final argv element, no stdin
    assert argv[-1] == "NARRATE THIS"
    assert stdin_text is None


def test_copilot_command_is_stdin_mode() -> None:
    argv, stdin_text = build_narration_command("copilot", "/usr/bin/copilot", "NARRATE THIS")
    assert stdin_text == "NARRATE THIS"
    assert "NARRATE THIS" not in argv


def test_model_override_is_applied() -> None:
    argv, _ = build_narration_command(
        "claude", "/usr/bin/claude", "x", model="claude-sonnet-4-6"
    )
    i = argv.index("--model")
    assert argv[i + 1] == "claude-sonnet-4-6"


def test_unknown_cli_raises() -> None:
    with pytest.raises(KeyError):
        build_narration_command("totally-unknown", "/usr/bin/x", "x")
