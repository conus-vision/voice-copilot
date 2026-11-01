import asyncio
import sys

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


def test_resize_follower_pushes_a_new_terminal_size_to_the_child(monkeypatch) -> None:
    # The child PTY is sized once at spawn; a window resized afterwards has to
    # reach it or the TUI keeps drawing for the old width and height.
    from voice_copilot.adapters import pty_adapter

    sizes = iter([(24, 80), (24, 80), (40, 120)])
    monkeypatch.setattr(pty_adapter, "_terminal_size", lambda: next(sizes))
    monkeypatch.setattr(pty_adapter, "_RESIZE_POLL_S", 0.0)

    class _Child:
        def __init__(self) -> None:
            self.sizes: list[tuple[int, int]] = []

        def setwinsize(self, rows: int, cols: int) -> None:
            self.sizes.append((rows, cols))

    child = _Child()
    follower = pty_adapter._ResizeFollower(child)  # reads (24, 80)
    assert follower.poll() is True  # unchanged
    assert follower.poll() is True  # (40, 120)
    assert child.sizes == [(40, 120)]


def _spawn_unpumped(argv: list[str]):  # type: ignore[no-untyped-def]
    """Start a PtyAdapter child without the pump, so nothing reaps it early."""
    import asyncio

    from voice_copilot.adapters.pty_adapter import PtyAdapter
    from voice_copilot.core.bus import EventBus

    adapter = PtyAdapter(EventBus(), argv)
    adapter._pump = lambda: None  # type: ignore[method-assign]
    asyncio.run(adapter.start())
    return adapter


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process semantics")
def test_stop_after_the_cli_exited_on_its_own_does_not_raise() -> None:
    # The usual end of a `vc` session: the CLI quits, the pump sees EOF
    # before anyone reaps it, then stop() runs. kill_process_tree reaps the
    # zombie and ptyprocess's isalive() used to raise PtyProcessError.
    import asyncio
    import time

    import psutil

    adapter = _spawn_unpumped(["sh", "-c", "exit 0"])
    deadline = time.monotonic() + 5
    while psutil.Process(adapter._child.pid).status() != psutil.STATUS_ZOMBIE:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    asyncio.run(adapter.stop())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_stop_takes_down_what_the_cli_left_running(tmp_path) -> None:
    # A worker the CLI forked, detached from SIGHUP, outlives the CLI itself
    # (codex sub-agents do). It is no longer a descendant, only a member of
    # the CLI's process group.
    import asyncio
    import time

    import psutil

    pid_file = tmp_path / "worker.pid"
    # The worker writes its own pid once nohup has taken hold, and the CLI
    # waits for it: exiting any sooner, the session leader's SIGHUP can reach
    # the worker before nohup ignores it (it did on CI runners).
    adapter = _spawn_unpumped(
        [
            "sh",
            "-c",
            f"nohup sh -c 'echo $$ > {pid_file}; exec sleep 300' >/dev/null 2>&1 & "
            f"while [ ! -s {pid_file} ]; do sleep 0.05; done; exit 0",
        ]
    )
    deadline = time.monotonic() + 5
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    worker = psutil.Process(int(pid_file.read_text()))
    # Stop only once the CLI has exited: before that the worker is still its
    # descendant and would go down with the process tree, sweep or no sweep.
    while worker.ppid() == adapter._child.pid:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    asyncio.run(adapter.stop())
    worker.wait(timeout=5)
    assert not worker.is_running() or worker.status() == psutil.STATUS_ZOMBIE
