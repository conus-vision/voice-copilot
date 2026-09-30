"""The proxy stays on loopback even when the panel is served to the network."""

from __future__ import annotations

import pytest

from voice_copilot.proxy.server import proxy_bind_host


def test_proxy_listens_on_loopback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_COPILOT_HOST", "0.0.0.0")
    monkeypatch.delenv("VOICE_COPILOT_PROXY_HOST", raising=False)
    assert proxy_bind_host() == "127.0.0.1"


def test_a_deliberate_override_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_COPILOT_PROXY_HOST", "10.0.0.5")
    assert proxy_bind_host() == "10.0.0.5"
