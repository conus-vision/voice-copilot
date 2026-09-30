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


def test_dotenv_values_stay_out_of_child_processes(monkeypatch, tmp_path) -> None:
    # `vc claude` in an app repo: the app's .env must not reach the agent
    # (an ANTHROPIC_API_KEY there would switch Claude Code to API billing).
    from voice_copilot.core import child_env as child_env_mod

    (tmp_path / ".env").write_text(f"{_DOTENV_VAR}=from-file\n", encoding="utf-8")
    os.environ.pop(_DOTENV_VAR, None)
    monkeypatch.setattr(child_env_mod, "_from_dotenv", {})
    monkeypatch.setenv("VOICE_COPILOT_TEST_SHELL_VAR", "from-shell")
    try:
        _run_main_in(tmp_path, monkeypatch)
        assert os.environ.get(_DOTENV_VAR) == "from-file"  # voice-copilot itself sees it
        env = child_env_mod.child_env({"ANTHROPIC_BASE_URL": "http://127.0.0.1:1/anthropic"})
        assert _DOTENV_VAR not in env
        assert env["VOICE_COPILOT_TEST_SHELL_VAR"] == "from-shell"
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1/anthropic"
    finally:
        os.environ.pop(_DOTENV_VAR, None)


def _captured_vc_args(monkeypatch, argv: list[str]) -> dict:  # type: ignore[type-arg]
    from typer.testing import CliRunner

    import voice_copilot.cli as cli_module

    seen: dict = {}  # type: ignore[type-arg]

    async def fake_run_vc(**kwargs) -> None:  # type: ignore[no-untyped-def]
        seen.update(kwargs)

    monkeypatch.setattr(cli_module, "_run_vc", fake_run_vc)
    result = CliRunner().invoke(cli_module.app, argv)
    assert result.exit_code == 0, result.output
    return seen


def test_vc_hands_unknown_options_to_the_cli(monkeypatch) -> None:
    seen = _captured_vc_args(monkeypatch, ["vc", "claude", "--resume", "-p", "hi"])
    assert seen["name"] == "claude"
    assert seen["cli_args"] == ["--resume", "-p", "hi"]


def test_vc_still_parses_its_own_options(monkeypatch) -> None:
    seen = _captured_vc_args(monkeypatch, ["vc", "codex", "--no-open", "--port", "9123"])
    assert seen["open_browser"] is False
    assert seen["port"] == 9123
    assert seen["cli_args"] == []


def test_run_codex_with_proxy_points_codex_at_the_proxy(monkeypatch, tmp_path) -> None:
    # codex ignores OPENAI_BASE_URL for model traffic; without the config flag
    # nothing reached the proxy while the adapter's own events were muted.
    from typer.testing import CliRunner

    import voice_copilot.cli as cli_module
    from voice_copilot.core.bus import EventBus

    monkeypatch.setattr(cli_module, "load_config", lambda: cli_module.Config())
    built: dict = {}  # type: ignore[type-arg]

    async def fake_run_with_adapter(*, build_adapter, **_) -> None:  # type: ignore[no-untyped-def]
        built["adapter"] = build_adapter(EventBus())

    monkeypatch.setattr(cli_module, "_run_with_adapter", fake_run_with_adapter)
    result = CliRunner().invoke(
        cli_module.app, ["run", "codex", "-p", "hi", "--proxy", "--proxy-port", "8799"]
    )
    assert result.exit_code == 0, result.output
    args = built["adapter"]._extra_args
    i = args.index("-c")
    assert args[i + 1] == 'openai_base_url="http://127.0.0.1:8799/openai-chatgpt"'


def test_every_subcommand_is_dispatched_as_itself() -> None:
    # A hard-coded list once missed a new subcommand, which then launched as
    # `vc integrate` and failed with "command not found".
    for name in ("integrate", "config", "version", "serve", "proxy", "run", "vc"):
        assert _normalize_argv(["voice-copilot", name]) == ["voice-copilot", name]
