import os

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


def test_main_calls_ensure_vc_alias(monkeypatch) -> None:
    import voice_copilot.cli as cli_module

    calls = []
    monkeypatch.setattr(cli_module, "ensure_vc_alias", lambda: calls.append(True))
    monkeypatch.setattr(cli_module, "app", lambda: None)
    monkeypatch.setattr("sys.argv", ["voice-copilot", "version"])

    cli_module.main()

    assert calls == [True]


_DOTENV_VAR = "VOICE_COPILOT_TEST_DOTENV"


def _run_main_in(tmp_path, monkeypatch) -> None:
    import voice_copilot.cli as cli_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "ensure_vc_alias", lambda: None)
    monkeypatch.setattr(cli_module, "app", lambda: None)
    monkeypatch.setattr("sys.argv", ["voice-copilot", "version"])
    cli_module.main()


def test_main_loads_dotenv_from_cwd(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text(f"{_DOTENV_VAR}=from-file", encoding="utf-8")
    os.environ.pop(_DOTENV_VAR, None)
    try:
        _run_main_in(tmp_path, monkeypatch)
        assert os.environ.get(_DOTENV_VAR) == "from-file"
    finally:
        os.environ.pop(_DOTENV_VAR, None)


def test_main_dotenv_does_not_override_shell_exports(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text(f"{_DOTENV_VAR}=from-file", encoding="utf-8")
    monkeypatch.setenv(_DOTENV_VAR, "from-shell")

    _run_main_in(tmp_path, monkeypatch)

    assert os.environ[_DOTENV_VAR] == "from-shell"
