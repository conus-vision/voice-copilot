import itertools

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
    # tool-free and leaves no session behind
    i = argv.index("--disallowedTools")
    assert argv[i + 1] == "*"
    assert "--no-session-persistence" in argv
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


def test_copilot_narrator_can_never_run_a_shell_or_write_files() -> None:
    # The narrator reads text the agent wrote; an injected instruction must
    # not turn into a command. A deny rule beats the blanket tool approval.
    from voice_copilot.providers.llm.copilot_cli import _BASE_FLAGS

    for args in (NARRATION_PROFILES["copilot"].args, _BASE_FLAGS):
        assert "--allow-all" not in args and "--yolo" not in args
        pairs = list(itertools.pairwise(args))
        assert ("--deny-tool", "shell") in pairs
        assert ("--deny-tool", "write") in pairs
