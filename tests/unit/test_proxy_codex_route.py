"""Codex, signed in with a ChatGPT plan, was never narrated.

Three separate reasons, each verified here: it ignores OPENAI_BASE_URL (so the
endpoint must arrive as a `-c` config override), it talks to the ChatGPT
backend rather than api.openai.com, and it prefers a WebSocket for /responses
that this proxy has to refuse so the client falls back to parseable HTTP.
"""

from __future__ import annotations

import json

import pytest
import zstandard
from fastapi.testclient import TestClient

from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import Config, load_config
from voice_copilot.proxy.body_sniffer import extract_user_query
from voice_copilot.proxy.cli_shims import (
    _render_cmd_shim,
    _render_shell_shim,
    resolve_cli_for_vc,
)
from voice_copilot.proxy.server import _PROVIDERS, _sniffable_body, base_urls_for, create_proxy_app


@pytest.fixture
def cfg(tmp_path) -> Config:
    return load_config(tmp_path / "missing.yaml")


# --- routing ----------------------------------------------------------


def test_chatgpt_route_targets_the_chatgpt_backend() -> None:
    upstream, parser = _PROVIDERS["openai-chatgpt"]
    # Same Responses wire format as the public API, different host: a plan
    # login's OAuth bearer is not valid at api.openai.com.
    assert upstream == "https://chatgpt.com/backend-api/codex"
    assert parser == "openai"
    assert base_urls_for("127.0.0.1", 8766)["OPENAI_CHATGPT_BASE_URL"] == (
        "http://127.0.0.1:8766/openai-chatgpt"
    )


def test_codex_launches_with_the_endpoint_as_a_config_flag(cfg, monkeypatch) -> None:
    monkeypatch.setattr(
        "voice_copilot.proxy.cli_shims._resolve_binary_path",
        lambda command, override, shim_dir: f"/usr/bin/{command}",
    )
    resolved = resolve_cli_for_vc("codex", cfg, port=8766)
    assert resolved is not None
    assert resolved.provider == "openai-chatgpt"
    # The env var is a no-op for codex's model traffic — the flag is what works.
    assert resolved.launch_args == (
        "-c",
        'openai_base_url="http://127.0.0.1:8766/openai-chatgpt"',
    )


def test_stale_configs_move_codex_onto_the_chatgpt_route(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("{}\n", encoding="utf-8")
    (tmp_path / "proxy-cli.yaml").write_text(
        "profiles:\n  codex:\n    provider: openai\n    base_url_env: OPENAI_BASE_URL\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.proxy_cli.profiles["codex"].provider == "openai-chatgpt"


def test_shims_carry_the_launch_flag() -> None:
    args = ("-c", 'openai_base_url="http://127.0.0.1:8766/openai-chatgpt"')
    cmd = _render_cmd_shim(binary_path=r"C:\bin\codex.cmd", env_overrides={}, launch_args=args)
    # cmd eats bare double quotes, so the inner ones must survive doubled.
    assert '"-c" "openai_base_url=""http://127.0.0.1:8766/openai-chatgpt"""' in cmd
    assert cmd.rstrip().endswith("%*")

    sh = _render_shell_shim(binary_path="/usr/bin/codex", env_overrides={}, launch_args=args)
    assert "'openai_base_url=\"http://127.0.0.1:8766/openai-chatgpt\"'" in sh
    assert sh.rstrip().endswith('"$@"')


# --- transport --------------------------------------------------------


def test_websocket_upgrade_is_refused_so_the_client_falls_back() -> None:
    client = TestClient(create_proxy_app(EventBus()))
    res = client.post(
        "/openai-chatgpt/responses",
        headers={"upgrade": "websocket", "connection": "Upgrade"},
        content=b"",
    )
    # 426 is what makes Codex retry over plain streaming HTTP, which we parse.
    assert res.status_code == 426


def test_zstd_request_bodies_are_readable_for_sniffing() -> None:
    payload = json.dumps({"input": [{"role": "user", "content": "hi"}]}).encode()
    packed = zstandard.ZstdCompressor().compress(payload)
    assert _sniffable_body({"content-encoding": "zstd"}, packed) == payload
    # Undeclared or undecodable bodies come back untouched, never raising.
    assert _sniffable_body({}, packed) == packed
    assert _sniffable_body({"content-encoding": "zstd"}, b"not zstd") == b"not zstd"


# --- query anchor -----------------------------------------------------


def test_responses_api_input_yields_the_user_query() -> None:
    # Codex sends the turn as Responses `input` items, not `messages`, and
    # prepends its own boilerplate user item — the real prompt is the last one.
    body = json.dumps(
        {
            "input": [
                {"type": "message", "role": "developer", "content": "system stuff"},
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "<recommended_plugins>…"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Say the word papaya."}],
                },
            ]
        }
    ).encode()
    got = extract_user_query(body, provider="openai-chatgpt", path="responses")
    assert got == "Say the word papaya."


def test_chat_completions_still_wins_when_both_are_present() -> None:
    body = json.dumps(
        {
            "messages": [{"role": "user", "content": "from messages"}],
            "input": [{"role": "user", "content": "from input"}],
        }
    ).encode()
    assert extract_user_query(body, provider="openai", path="v1/chat/completions") == (
        "from messages"
    )


# --- narration profile ------------------------------------------------


def test_codex_narration_command_is_stripped_down() -> None:
    from voice_copilot.commentator.cli_profiles import build_narration_command

    argv, stdin_text = build_narration_command(
        "codex", "/usr/bin/codex", "be brief", "the agent read README.md"
    )
    # The prompt rides stdin: as a positional arg the Windows .cmd wrapper
    # mangles a long multi-line Cyrillic prompt and codex answers something
    # unrelated (invented prose, "no input data was provided" summaries).
    assert stdin_text is not None
    assert "the agent read README.md" in stdin_text
    assert "be brief" in stdin_text
    # A ChatGPT plan rejects API-only slugs outright, which is how the
    # commentator went silent; and the user's own config would drag in their
    # heavy model, ultra reasoning, MCP servers and a session file per line.
    assert argv[:3] == ["/usr/bin/codex", "--model", "gpt-5.4-mini"]
    for flag in ("exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check"):
        assert flag in argv
    assert not any("the agent read README.md" in a for a in argv)
