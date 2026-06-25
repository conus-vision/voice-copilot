import pytest

from voice_copilot.core.config import Config, ProxyCliProfileConfig, load_config
from voice_copilot.proxy.cli_shims import resolve_cli_for_vc


@pytest.fixture
def cfg(tmp_path) -> Config:
    return load_config(tmp_path / "missing.yaml")


def test_resolves_known_catalog_entry_by_command(cfg, monkeypatch) -> None:
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: f"/usr/bin/{command}" if command == "claude" else None,
    )
    resolved = resolve_cli_for_vc("claude", cfg, port=8766)
    assert resolved is not None
    assert resolved.profile_id == "claude"
    assert resolved.resolved_binary == "/usr/bin/claude"
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
