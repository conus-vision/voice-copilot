"""The `terminal` (kind: shell) profile and the catalog invariants it relies on."""

import pytest

from voice_copilot.core.config import Config, ProxyRoute, load_config
from voice_copilot.proxy.cli_catalog import CLI_CATALOG
from voice_copilot.proxy.cli_shims import (
    _profile_from_config,
    _proxy_env_overrides,
    _render_powershell_launch,
    describe_cli_shims,
    install_cli_shim,
    resolve_cli_for_vc,
    restore_cli_shim,
)
from voice_copilot.proxy.server import base_urls_for


@pytest.fixture
def cfg(tmp_path) -> Config:
    return load_config(tmp_path / "missing.yaml")


def test_every_catalog_provider_is_a_known_route() -> None:
    routes = set(ProxyRoute.__args__)  # type: ignore[attr-defined]
    for profile_id, meta in CLI_CATALOG.items():
        assert meta.provider in routes, f"{profile_id} points at unknown route {meta.provider!r}"


def test_every_catalog_entry_has_launcher_metadata() -> None:
    for profile_id, meta in CLI_CATALOG.items():
        assert meta.label and meta.description, profile_id
        assert 1 <= len(meta.icon) <= 2, profile_id
        assert meta.accent.startswith("#"), profile_id


def test_catalog_order_is_unique_and_dense() -> None:
    orders = sorted(meta.order for meta in CLI_CATALOG.values())
    assert orders == list(range(len(CLI_CATALOG)))


def test_shell_profile_exports_every_route(cfg) -> None:
    meta = CLI_CATALOG["terminal"]
    overrides = _proxy_env_overrides(
        "terminal",
        _profile_from_config(cfg, "terminal"),
        meta=meta,
        host="127.0.0.1",
        port=8766,
    )
    urls = base_urls_for("127.0.0.1", 8766)
    assert overrides["ANTHROPIC_BASE_URL"] == urls["ANTHROPIC_BASE_URL"]
    assert overrides["OPENAI_BASE_URL"] == urls["OPENAI_BASE_URL"]
    assert overrides["DEEPSEEK_BASE_URL"] == urls["DEEPSEEK_BASE_URL"]
    # gemini-cli reads the GOOGLE_ prefixed spelling.
    assert overrides["GOOGLE_GEMINI_BASE_URL"] == urls["GEMINI_BASE_URL"]


def test_shell_profile_has_no_path_shim(cfg, tmp_path) -> None:
    status = describe_cli_shims(cfg, port=8766)
    terminal = next(p for p in status["profiles"] if p["id"] == "terminal")
    assert terminal["kind"] == "shell"
    assert terminal["shim_path"] is None
    assert terminal["installed"] is False

    with pytest.raises(RuntimeError, match="no PATH shim"):
        install_cli_shim("terminal", cfg, port=8766)
    with pytest.raises(RuntimeError, match="no PATH shim"):
        restore_cli_shim("terminal", cfg, port=8766)


def test_powershell_launch_without_binary_just_opens_the_shell(tmp_path) -> None:
    command = _render_powershell_launch(
        binary_path=None,
        env_overrides={"OPENAI_BASE_URL": "http://127.0.0.1:8766/openai/v1"},
        working_directory=tmp_path,
        title="voice-copilot - Terminal",
    )
    assert "$env:OPENAI_BASE_URL" in command
    assert "Set-Location" in command
    assert "& '" not in command


def test_vc_terminal_resolves_to_a_shell(cfg, monkeypatch) -> None:
    monkeypatch.setattr("voice_copilot.proxy.cli_shims._shell_path", lambda: "/bin/bash")
    resolved = resolve_cli_for_vc("terminal", cfg, port=8766)
    assert resolved is not None
    assert resolved.resolved_binary == "/bin/bash"
    assert resolved.env_overrides["OPENAI_BASE_URL"] == "http://127.0.0.1:8766/openai/v1"


def test_deepseek_route_is_reachable(cfg, monkeypatch) -> None:
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: f"/usr/bin/{command}",
    )
    resolved = resolve_cli_for_vc("dsh", cfg, port=8766)
    assert resolved is not None
    # The harness speaks OpenAI's dialect, so it reads OPENAI_BASE_URL — but the
    # value points at the deepseek route, whose upstream is api.deepseek.com.
    assert resolved.env_overrides == {"OPENAI_BASE_URL": "http://127.0.0.1:8766/deepseek/v1"}
