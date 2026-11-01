import pytest

from voice_copilot.commentator.cli_profiles import (
    NARRATION_PROFILES,
    build_narration_command,
    profile_needs_system_file,
)


def test_known_clis_have_profiles() -> None:
    assert set(NARRATION_PROFILES) >= {"claude", "codex", "opencode", "gemini", "copilot"}


def test_profile_needs_system_file() -> None:
    assert profile_needs_system_file("claude") is True
    assert profile_needs_system_file("copilot") is False


def test_claude_passes_system_via_file_and_user_via_stdin() -> None:
    argv, stdin_text = build_narration_command(
        "claude", "/usr/bin/claude", "SYSTEM", "USER EVENTS", system_file_path="/tmp/sys.txt"
    )
    assert argv[0] == "/usr/bin/claude"
    assert "--model" in argv
    # narrator system prompt is delivered as a file path, not inline
    i = argv.index("--system-prompt-file")
    assert argv[i + 1] == "/tmp/sys.txt"
    assert "--exclude-dynamic-system-prompt-sections" in argv
    assert "-p" in argv
    # user events go to stdin (stdin mode); neither system text nor user text is in argv
    assert stdin_text == "USER EVENTS"
    assert "USER EVENTS" not in argv
    assert "SYSTEM" not in argv


def test_copilot_flattens_system_and_user_to_stdin() -> None:
    argv, stdin_text = build_narration_command("copilot", "/usr/bin/copilot", "SYSTEM", "USER")
    # no system flag → system + user flattened, fed via stdin
    assert stdin_text == "SYSTEM\n\nUSER"
    assert "USER" not in argv
    assert "--system-prompt-file" not in argv


def test_model_override_is_applied() -> None:
    argv, _ = build_narration_command(
        "claude", "/usr/bin/claude", "s", "u", model="claude-sonnet-4-6", system_file_path="/tmp/s"
    )
    i = argv.index("--model")
    assert argv[i + 1] == "claude-sonnet-4-6"


def test_unknown_cli_raises() -> None:
    with pytest.raises(KeyError):
        build_narration_command("totally-unknown", "/usr/bin/x", "s", "u")


def test_pi_narrates_with_the_users_own_default_model_and_nothing_else() -> None:
    argv, stdin_text = build_narration_command(
        "pi", "pi", "be brief", "what happened", system_file_path="sys.txt"
    )
    assert "--model" not in argv
    assert argv[argv.index("--system-prompt") + 1] == "sys.txt"
    for flag in ("--no-tools", "--no-extensions", "--no-session", "--no-context-files"):
        assert flag in argv
    assert stdin_text == "what happened"
