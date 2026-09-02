import asyncio

import pytest

from voice_copilot.adapters.pty_adapter import PtyAdapter
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import EventKind


class _FakeChild:
    """Stands in for winpty/ptyprocess PtyProcess. `read` raises EOFError
    immediately so the pump loop exits at once without touching the real
    terminal (and `isalive()` stays True so send/stop still have a live
    child to act on, matching how a real child behaves mid-session)."""

    def __init__(self) -> None:
        self.pid = 4242
        self.written: list[str] = []
        self.terminated = False
        self._alive = True

    def read(self, size: int = 1024) -> str:
        raise EOFError

    def isalive(self) -> bool:
        return self._alive

    def write(self, data: str) -> int:
        self.written.append(data)
        return len(data)

    def terminate(self, force: bool = False) -> None:
        self.terminated = True
        self._alive = False


@pytest.fixture
def fake_pty(monkeypatch):
    child = _FakeChild()
    spawn_calls: list[dict[str, object]] = []

    class _FakePtyProcess:
        @staticmethod
        def spawn(argv, cwd=None, env=None, dimensions=(24, 80)):
            spawn_calls.append({"argv": argv, "cwd": cwd, "env": env})
            return child

    monkeypatch.setattr("voice_copilot.adapters.pty_adapter._PtyProcess", _FakePtyProcess)
    return child, spawn_calls


@pytest.mark.asyncio
async def test_start_spawns_child_and_publishes_session_started(fake_pty) -> None:
    _, spawn_calls = fake_pty
    bus = EventBus()
    adapter = PtyAdapter(bus, ["claude", "--flag"], env={"ANTHROPIC_BASE_URL": "http://x"})

    async with bus.subscribe() as q:
        await adapter.start()
        event = await asyncio.wait_for(q.get(), timeout=1)

    assert event.kind == EventKind.SESSION_STARTED
    assert spawn_calls == [
        {"argv": ["claude", "--flag"], "cwd": None, "env": {"ANTHROPIC_BASE_URL": "http://x"}}
    ]
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_user_message_writes_line_to_child(fake_pty) -> None:
    child, _ = fake_pty
    bus = EventBus()
    adapter = PtyAdapter(bus, ["claude"])
    await adapter.start()
    await adapter.send_user_message("hello")
    # winpty takes str, ptyprocess takes bytes; the adapter encodes on POSIX.
    written = [w.decode() if isinstance(w, bytes) else w for w in child.written]
    assert written == ["hello\r"]
    await adapter.stop()


@pytest.mark.asyncio
async def test_stop_terminates_child(fake_pty) -> None:
    child, _ = fake_pty
    bus = EventBus()
    adapter = PtyAdapter(bus, ["claude"])
    await adapter.start()
    await adapter.stop()
    assert child.terminated is True


@pytest.mark.asyncio
async def test_exit_task_completes_once_child_exits(fake_pty) -> None:
    bus = EventBus()
    adapter = PtyAdapter(bus, ["claude"])
    await adapter.start()
    task = adapter.exit_task()
    assert task is not None
    await asyncio.wait_for(task, timeout=1)
    await adapter.stop()
