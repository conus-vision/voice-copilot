"""Session-change listeners may read the registry back without deadlocking."""

from __future__ import annotations

import threading

from voice_copilot.proxy.session import SessionRegistry


def test_a_listener_can_read_the_registry_when_a_session_appears() -> None:
    reg = SessionRegistry()
    seen: list[int] = []
    reg.on_change(lambda: seen.append(len(reg.all())))

    worker = threading.Thread(
        target=lambda: reg.identify({"user-agent": "claude-cli/2.0"}, provider="anthropic"),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "identify() deadlocked inside its own listener"
    assert seen == [1]
