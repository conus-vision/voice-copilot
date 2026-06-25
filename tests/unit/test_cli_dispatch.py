from voice_copilot.cli import _normalize_argv


def test_unknown_name_gets_vc_inserted() -> None:
    assert _normalize_argv(["voice-copilot", "claude"]) == ["voice-copilot", "vc", "claude"]


def test_known_subcommand_passes_through() -> None:
    assert _normalize_argv(["voice-copilot", "serve"]) == ["voice-copilot", "serve"]


def test_explicit_vc_passes_through() -> None:
    assert _normalize_argv(["voice-copilot", "vc", "claude"]) == ["voice-copilot", "vc", "claude"]


def test_flag_passes_through() -> None:
    assert _normalize_argv(["voice-copilot", "--help"]) == ["voice-copilot", "--help"]


def test_bare_invocation_passes_through() -> None:
    assert _normalize_argv(["voice-copilot"]) == ["voice-copilot"]


def test_extra_args_after_name_are_preserved() -> None:
    assert _normalize_argv(["voice-copilot", "claude", "--", "-p", "hi"]) == [
        "voice-copilot",
        "vc",
        "claude",
        "--",
        "-p",
        "hi",
    ]
