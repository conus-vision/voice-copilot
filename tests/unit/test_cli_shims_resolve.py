import pytest

from voice_copilot.core.config import Config, ProxyCliProfileConfig, load_config
from voice_copilot.proxy.cli_shims import resolve_cli_for_vc


@pytest.fixture
def cfg(tmp_path) -> Config:
    return load_config(tmp_path / "missing.yaml")


def test_resolves_known_catalog_entry_by_command(cfg, monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: f"/usr/bin/{command}" if command == "claude" else None,
    )
    resolved = resolve_cli_for_vc("claude", cfg, port=8766)
    assert resolved is not None
    assert resolved.profile_id == "claude"
    assert resolved.resolved_binary == "/usr/bin/claude"
    # Claude Code stops deferring MCP/system-tool schemas behind a custom
    # ANTHROPIC_BASE_URL unless ENABLE_TOOL_SEARCH is set, inflating the context
    # window by tens of K tokens — so wrapping claude must turn deferral back on.
    assert resolved.env_overrides == {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8766/anthropic",
        "ENABLE_TOOL_SEARCH": "true",
    }


def test_claude_tool_search_respects_existing_user_value(cfg, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "auto:50")
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: f"/usr/bin/{command}",
    )
    resolved = resolve_cli_for_vc("claude", cfg, port=8766)
    assert resolved is not None
    # The user's own Claude Code knob wins; we do not clobber it.
    assert "ENABLE_TOOL_SEARCH" not in resolved.env_overrides


def test_non_claude_anthropic_profile_gets_no_tool_search(cfg, monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: f"/usr/bin/{command}",
    )
    # aider is an anthropic-provider profile but not Claude Code — the
    # ENABLE_TOOL_SEARCH knob is Claude-Code-specific, so it must not leak here.
    resolved = resolve_cli_for_vc("aider", cfg, port=8766)
    assert resolved is not None
    assert resolved.env_overrides == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8766/anthropic"}


def test_resolves_known_catalog_entry_by_profile_id_using_real_command(cfg, monkeypatch) -> None:
    seen_commands = []

    def fake_resolve(command, override, shim_dir):
        seen_commands.append(command)
        return f"/usr/bin/{command}" if command == "cn" else None

    monkeypatch.setattr("voice_copilot.proxy.cli_shims._resolve_binary_path", fake_resolve)

    resolved = resolve_cli_for_vc("continue", cfg, port=8766)
    assert resolved is not None
    assert resolved.resolved_binary == "/usr/bin/cn"
    assert seen_commands == ["cn"]


def test_resolves_known_catalog_entry_by_typed_command_too(cfg, monkeypatch) -> None:
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: f"/usr/bin/{command}" if command == "cn" else None,
    )
    resolved = resolve_cli_for_vc("cn", cfg, port=8766)
    assert resolved is not None
    assert resolved.profile_id == "continue"
    assert resolved.resolved_binary == "/usr/bin/cn"


def test_resolves_user_added_profile_not_in_catalog(cfg, monkeypatch) -> None:
    cfg.proxy_cli.profiles["mytool"] = ProxyCliProfileConfig(
        provider="openai", base_url_env="OPENAI_BASE_URL"
    )
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: "/usr/bin/mytool" if command == "mytool" else None,
    )
    resolved = resolve_cli_for_vc("mytool", cfg, port=8766)
    assert resolved is not None
    assert resolved.profile_id == "mytool"
    assert resolved.env_overrides == {"OPENAI_BASE_URL": "http://127.0.0.1:8766/openai/v1"}


def test_returns_none_for_unknown_name(cfg) -> None:
    assert resolve_cli_for_vc("totally-unknown-cli", cfg, port=8766) is None


def test_raises_when_binary_not_found(cfg, monkeypatch) -> None:
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: None,
    )
    with pytest.raises(RuntimeError, match="could not resolve"):
        resolve_cli_for_vc("claude", cfg, port=8766)


def test_vc_runs_in_the_shells_directory_not_the_panels(cfg, monkeypatch, tmp_path) -> None:
    panel_folder = tmp_path / "projA"
    shell_folder = tmp_path / "projB"
    panel_folder.mkdir()
    shell_folder.mkdir()
    cfg.proxy_cli.working_directory = str(panel_folder)  # picked in the Launch tab
    monkeypatch.chdir(shell_folder)
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: f"/usr/bin/{command}",
    )
    resolved = resolve_cli_for_vc("claude", cfg, port=8766)
    assert resolved is not None
    assert resolved.working_directory == shell_folder.resolve()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://api.z.ai/api/anthropic", "https://api.z.ai/api/anthropic"),
        ("https://api.z.ai/api/anthropic/", "https://api.z.ai/api/anthropic"),
        ("http://127.0.0.1:8766/anthropic", None),  # already a voice-copilot route
        ("not a url", None),
        ("", None),
    ],
)
def test_user_upstream_for_the_anthropic_route(value: str, expected: str | None) -> None:
    from voice_copilot.proxy.cli_shims import _user_upstream

    assert _user_upstream(value, proxy_url="http://127.0.0.1:1/anthropic") == expected


def test_user_upstream_drops_v1_for_routes_that_add_it() -> None:
    from voice_copilot.proxy.cli_shims import _user_upstream

    got = _user_upstream("https://openrouter.ai/api/v1", proxy_url="http://127.0.0.1:1/openai/v1")
    assert got == "https://openrouter.ai/api"


def test_vc_keeps_a_base_url_the_user_already_set(cfg, monkeypatch) -> None:
    # Claude Code pointed at an Anthropic-compatible vendor: overwriting the
    # variable sent that vendor's token to api.anthropic.com.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: f"/usr/bin/{command}",
    )
    resolved = resolve_cli_for_vc("claude", cfg, port=8766)
    assert resolved is not None
    assert resolved.upstream == "https://api.z.ai/api/anthropic"
    assert resolved.upstream_env == "ANTHROPIC_BASE_URL"
    # The CLI itself still talks to the proxy.
    assert resolved.env_overrides["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8766/anthropic"


def test_a_cli_that_ignores_the_variable_gets_no_upstream_from_it(cfg, monkeypatch) -> None:
    # codex takes its endpoint from a config flag, not OPENAI_BASE_URL.
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: f"/usr/bin/{command}",
    )
    resolved = resolve_cli_for_vc("codex", cfg, port=8766)
    assert resolved is not None
    assert resolved.upstream is None


def test_dsh_keeps_a_deepseek_base_url_the_user_already_set(cfg, monkeypatch) -> None:
    # dsh's launch args are a subcommand (`web`), not the endpoint: its own
    # $DEEPSEEK_BASE_URL still names the upstream.
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek.internal.example/anthropic")
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: f"/usr/bin/{command}",
    )
    resolved = resolve_cli_for_vc("dsh", cfg, port=8766)
    assert resolved is not None
    assert resolved.upstream == "https://deepseek.internal.example/anthropic"
