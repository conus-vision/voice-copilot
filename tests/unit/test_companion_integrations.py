"""Installing and removing the plugins and hook entries, in a throwaway home."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voice_copilot.companion import integrations


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(integrations, "_home", lambda: tmp_path)
    monkeypatch.setattr(integrations, "data_dir", lambda: tmp_path / "vc-data")
    for var in (
        "PI_CODING_AGENT_DIR",
        "XDG_CONFIG_HOME",
        "HERMES_HOME",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    # A stable forwarder command, whatever the test machine has on PATH.
    monkeypatch.setattr(integrations.shutil, "which", lambda name: f"/usr/bin/{name}")
    return tmp_path


def test_json_hooks_merge_keeps_the_users_own_hooks(home: Path) -> None:
    settings = home / ".gemini" / "settings.json"
    settings.parent.mkdir(parents=True)
    mine = {"type": "command", "command": "my-linter", "timeout": 5000}
    settings.write_text(json.dumps({"theme": "dark", "hooks": {"BeforeTool": [{"hooks": [mine]}]}}))

    gemini = integrations.get("gemini")
    assert not gemini.installed()
    gemini.install()
    gemini.install()  # idempotent
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    before_tool = data["hooks"]["BeforeTool"]
    assert before_tool[0]["hooks"][0] == mine
    ours = before_tool[1]
    assert len(before_tool) == 2
    assert ours["matcher"] == ".*"
    assert ours["hooks"][0]["command"] == "/usr/bin/voice-copilot-hook gemini"
    assert ours["hooks"][0]["timeout"] == integrations.GATE_TIMEOUT_S * 1000
    assert ours["hooks"][0]["name"] == "voice-copilot"
    assert gemini.installed()
    assert (home / ".gemini" / "settings.json.voice-copilot.bak").exists()

    gemini.uninstall()
    data = json.loads(settings.read_text())
    assert data == {"theme": "dark", "hooks": {"BeforeTool": [{"hooks": [mine]}]}}
    assert not gemini.installed()


def test_codex_hooks_name_the_cli_for_the_claude_dialect(home: Path) -> None:
    codex = integrations.get("codex")
    codex.install()
    data = json.loads((home / ".codex" / "hooks.json").read_text())
    command = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert command == "/usr/bin/voice-copilot-hook claude --cli codex"
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == integrations.GATE_TIMEOUT_S
    assert "matcher" not in data["hooks"]["UserPromptSubmit"][0]
    codex.uninstall()
    assert json.loads((home / ".codex" / "hooks.json").read_text()) == {}


def test_a_broken_settings_file_is_left_alone(home: Path) -> None:
    settings = home / ".qwen" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{ not json")
    with pytest.raises(integrations.IntegrationError, match="not valid JSON"):
        integrations.get("qwen").install()
    assert settings.read_text() == "{ not json"


def test_copilot_gets_its_own_hooks_file(home: Path) -> None:
    copilot = integrations.get("copilot")
    copilot.install()
    data = json.loads((home / ".copilot" / "hooks" / "voice-copilot.json").read_text())
    assert data["version"] == 1
    pre = data["hooks"]["preToolUse"][0]
    assert pre["bash"] == "/usr/bin/voice-copilot-hook copilot preToolUse"
    assert pre["timeoutSec"] == integrations.GATE_TIMEOUT_S
    copilot.uninstall()
    assert not copilot.installed()


def test_kimi_hooks_live_between_markers_in_config_toml(home: Path) -> None:
    config = home / ".kimi" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('default_model = "kimi-k2"\n')
    kimi = integrations.get("kimi")
    kimi.install()
    kimi.install()
    text = config.read_text()
    assert text.startswith('default_model = "kimi-k2"')
    assert text.count("# >>> voice-copilot hooks >>>") == 1
    assert 'event = "PreToolUse"' in text
    kimi.uninstall()
    assert config.read_text().strip() == 'default_model = "kimi-k2"'


def test_pi_extension_is_copied_where_pi_loads_it(home: Path) -> None:
    pi = integrations.get("pi")
    pi.install()
    target = home / ".pi" / "agent" / "extensions" / "voice-copilot.ts"
    assert target.read_text() == (integrations.ASSETS / "pi" / "voice-copilot.ts").read_text()
    assert pi.installed()
    pi.uninstall()
    assert not target.exists()


def test_hermes_plugin_folder(home: Path) -> None:
    hermes = integrations.get("hermes")
    hermes.install()
    target = home / ".hermes" / "plugins" / "voice_copilot"
    assert (target / "plugin.yaml").exists() and (target / "__init__.py").exists()
    hermes.uninstall()
    assert not target.exists()


def test_claude_plugin_posts_every_event_to_voice_copilot(tmp_path: Path) -> None:
    plugin = integrations.write_claude_plugin(
        tmp_path / "p", integrations.companion_url(8765), launch="8765-abc", mode="control"
    )
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "voice-copilot"
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text())["hooks"]
    assert "SessionStart" not in hooks  # Claude Code runs only command hooks there
    pre = hooks["PreToolUse"][0]
    assert pre["matcher"] == "*"
    entry = pre["hooks"][0]
    assert entry["type"] == "http"
    assert entry["url"] == (
        "http://127.0.0.1:8765/api/companion/v1/hooks/claude?cli=claude&launch=8765-abc&mode=control"
    )
    assert entry["timeout"] == integrations.GATE_TIMEOUT_S
    assert entry["headers"] == {
        "X-Voice-Copilot-Launch": "$VOICE_COPILOT_LAUNCH",
        "X-Voice-Copilot-Token": "$VOICE_COPILOT_TOKEN",
    }
    assert hooks["MessageDisplay"][0]["hooks"][0]["timeout"] <= 5


def test_claude_install_without_the_cli_explains_the_manual_route(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(integrations.shutil, "which", lambda name: None)
    with pytest.raises(integrations.IntegrationError, match="/plugin marketplace add"):
        integrations.get("claude").install(port=8765)
    marketplace = home / "vc-data" / "claude-marketplace"
    listing = json.loads((marketplace / ".claude-plugin" / "marketplace.json").read_text())
    assert listing["plugins"][0]["source"] == "./plugins/voice-copilot"


def test_claude_install_runs_the_plugin_commands(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> object:
        calls.append(argv)
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(integrations.subprocess, "run", fake_run)
    report = integrations.get("claude").install(port=9000)
    assert calls[0][1:4] == ["plugin", "marketplace", "add"]
    assert calls[1][1:] == ["plugin", "install", "voice-copilot@voice-copilot"]
    assert "port 9000" in report
    hooks = json.loads(
        (
            home
            / "vc-data"
            / "claude-marketplace"
            / "plugins"
            / "voice-copilot"
            / "hooks"
            / "hooks.json"
        ).read_text()
    )
    assert hooks["hooks"]["Stop"][0]["hooks"][0]["url"].startswith("http://127.0.0.1:9000/")


def test_session_wiring(home: Path) -> None:
    wiring = integrations.session_wiring("claude", port=8800, launch_id="8800-x", proxied=True)
    assert wiring.args[0] == "--plugin-dir"
    assert Path(wiring.args[1], "hooks", "hooks.json").exists()
    assert wiring.env == {
        "VOICE_COPILOT_URL": "http://127.0.0.1:8800/api/companion/v1",
        "VOICE_COPILOT_LAUNCH": "8800-x",
        "VOICE_COPILOT_MODE": "control",
    }
    pi = integrations.session_wiring("pi", port=8800, launch_id="8800-y", proxied=False)
    assert pi.args[0] == "-e" and pi.args[1].endswith("voice-copilot.ts")
    assert pi.env["VOICE_COPILOT_MODE"] == "narrate"
    other = integrations.session_wiring("codex", port=8800, launch_id="8800-z", proxied=True)
    assert other.args == [] and other.env["VOICE_COPILOT_LAUNCH"] == "8800-z"
    assert integrations.plugin_narrates("pi") and not integrations.plugin_narrates("claude")


def test_every_integration_describes_itself() -> None:
    for item in integrations.describe_all(port=8765):
        assert item["label"] and item["method"] and item["docs_url"].startswith("https://")
        assert item["command"] == f"voice-copilot integrate {item['id']}"


def test_shells_and_unknown_programs_get_no_wiring(home: Path) -> None:
    # A launch id in a shell would hide a permanently installed Claude plugin
    # for every claude started inside it.
    for name in ("terminal", "bash", "aider"):
        wiring = integrations.session_wiring(name, port=8800, launch_id="8800-x", proxied=True)
        assert wiring.args == [] and wiring.env == {}
