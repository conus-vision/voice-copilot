"""Regression: saving settings from the panel must not drop the running `auto`
commentator back to the saved API provider.

The panel POSTs the whole config; its `commentator.provider` block is whatever
the user last picked for `api` mode. Before the fix that block went straight to
the live Commentator, so one save turned a working reuse-the-CLI narrator into
an unconfigured API one and every batch died on a missing key.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from voice_copilot.cli import _make_commentator_resolver
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig, Config, ProviderConfig
from voice_copilot.proxy.cli_shims import ResolvedCli
from voice_copilot.web import server as web_server


def _resolved() -> ResolvedCli:
    return ResolvedCli(
        profile_id="claude",
        label="Claude Code",
        resolved_binary="/usr/bin/claude",
        env_overrides={},
        working_directory=None,
        provider="anthropic",
    )


def _saved_api_provider_config() -> Config:
    cfg = Config()  # commentator.mode defaults to "auto"
    cfg.commentator.provider = ProviderConfig(name="anthropic", options={"model": "haiku"})
    return cfg


def test_resolver_keeps_auto_for_a_freshly_saved_config() -> None:
    resolve = _make_commentator_resolver(_resolved(), "claude")
    commentator_cfg, notice = resolve(_saved_api_provider_config())
    assert commentator_cfg.provider.name == "auto"
    assert commentator_cfg.provider.options["cli"] == "claude"
    assert commentator_cfg.provider.options["binary"] == "/usr/bin/claude"
    assert "claude" in notice.lower()


def test_resolver_honors_a_switch_to_api_mode() -> None:
    resolve = _make_commentator_resolver(_resolved(), "claude")
    cfg = _saved_api_provider_config()
    cfg.commentator.mode = "api"
    commentator_cfg, notice = resolve(cfg)
    assert commentator_cfg.provider.name == "anthropic"
    assert commentator_cfg.provider.options["model"] == "haiku"
    assert "API" in notice


class _RecordingCommentator:
    def __init__(self) -> None:
        self.seen: list[CommentatorConfig] = []

    def update_config(self, cfg: CommentatorConfig, language: Any = None) -> None:
        self.seen.append(cfg)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(web_server, "save_config", lambda cfg: None)
    app = web_server.create_app(EventBus(), Config())
    # The panel only answers to loopback Host headers (see web/guard.py).
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_post_config_feeds_the_resolved_provider_to_the_commentator(client: TestClient) -> None:
    commentator = _RecordingCommentator()
    client.app.state.commentator = commentator
    client.app.state.commentator_resolver = _make_commentator_resolver(_resolved(), "claude")

    res = client.post("/api/config", json=_saved_api_provider_config().model_dump(mode="json"))
    assert res.status_code == 200
    # The response still carries the user's saved (API) provider…
    assert res.json()["commentator"]["provider"]["name"] == "anthropic"
    # …but the live commentator keeps narrating through the launched CLI.
    assert [c.provider.name for c in commentator.seen] == ["auto"]
    assert client.app.state.launch_notice is not None


def test_post_config_without_a_resolver_passes_the_saved_provider(client: TestClient) -> None:
    # `vc serve` (no launched CLI) has no resolver — the saved provider is all there is.
    commentator = _RecordingCommentator()
    client.app.state.commentator = commentator

    res = client.post("/api/config", json=_saved_api_provider_config().model_dump(mode="json"))
    assert res.status_code == 200
    assert [c.provider.name for c in commentator.seen] == ["anthropic"]


def test_a_provider_that_cannot_be_built_is_rejected_and_not_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `anthropic` takes no base_url: the old handler wrote this to disk first,
    # then failed, and every later start crashed building the commentator.
    saved: list[Config] = []
    monkeypatch.setattr(web_server, "save_config", saved.append)
    app = web_server.create_app(EventBus(), Config())
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    before = app.state.config

    cfg = Config()
    cfg.commentator.mode = "api"
    cfg.commentator.provider = ProviderConfig(
        name="anthropic", options={"model": "haiku", "base_url": "http://localhost:1234/v1"}
    )
    res = client.post("/api/config", json=cfg.model_dump(mode="json"))

    assert res.status_code == 400
    assert "base_url" in res.json()["detail"]
    assert saved == []
    assert app.state.config is before


def test_an_invalid_config_is_a_client_error(client: TestClient) -> None:
    res = client.post("/api/config", json={"commentator_language": "klingon"})
    assert res.status_code == 422


def _launching_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _RecordingCommentator]:
    monkeypatch.setattr(web_server, "save_config", lambda cfg: None)

    def fake_launch(profile_id: str, cfg: Config, **_: Any) -> dict[str, Any]:
        return {"ok": True, "profile_id": profile_id, "binary_path": f"/usr/bin/{profile_id}"}

    monkeypatch.setattr(web_server, "launch_cli_profile", fake_launch)
    app = web_server.create_app(EventBus(), Config(), proxy_port=8766)
    commentator = _RecordingCommentator()
    app.state.commentator = commentator
    return TestClient(app, base_url="http://127.0.0.1:8765"), commentator


def test_a_panel_launch_narrates_through_the_launched_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Quickstart promises no extra API key: Launch must switch an `auto`
    # commentator onto the CLI it just started, as `vc` does.
    client, commentator = _launching_client(monkeypatch)
    assert client.post("/api/proxy/cli-shims/claude/launch").status_code == 200

    provider = commentator.seen[-1].provider
    assert provider.name == "auto"
    assert provider.options == {"cli": "claude", "binary": "/usr/bin/claude"}
    assert "claude" in (client.app.state.launch_notice or "")

    # A later settings save keeps narrating through that CLI.
    res = client.post("/api/config", json=_saved_api_provider_config().model_dump(mode="json"))
    assert res.status_code == 200
    assert commentator.seen[-1].provider.name == "auto"


def test_launching_a_cli_that_cannot_narrate_keeps_the_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, commentator = _launching_client(monkeypatch)
    assert client.post("/api/proxy/cli-shims/terminal/launch").status_code == 200
    assert commentator.seen == []
