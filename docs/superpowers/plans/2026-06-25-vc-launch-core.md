# `vc` Launch Core (PTY + Resolution + Proxy) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `voice-copilot <name>` / `voice-copilot vc <name>` spawn `<name>` in a real, live terminal session (not headless), automatically proxying its API traffic when `<name>` matches the existing CLI catalog or a user-added config profile, with push-to-talk message injection working throughout.

**Architecture:** A new `PtyAdapter` (implementing the existing `CLIAdapter` interface) wraps `pexpect`/`wexpect` and hands the real terminal to the child process via `.interact()`. A new `resolve_cli_for_vc()` function in `proxy/cli_shims.py` reuses the existing `CLI_CATALOG`/`ProxyCliProfileConfig` machinery to compute env overrides for known CLIs. A new `vc` Typer command (plus argv pre-processing so the bare CLI name works without typing `vc`) wires the adapter, an auto-allocated per-instance proxy/panel, and exits the whole process once the wrapped terminal session ends — not just on Ctrl+C.

**Tech Stack:** Python 3.11+, `pexpect` (POSIX) / `wexpect` (Windows) for PTY control, existing FastAPI/uvicorn/asyncio stack, pytest + pytest-asyncio.

## Global Constraints

- Python 3.11+, formatted with `ruff format`, linted with `ruff`, `mypy` strict on new code.
- Package manager is `uv` (`uv sync`, `uv run voice-copilot ...`).
- No hidden retries, no silent fallbacks between providers/mechanisms — a failure must be surfaced to the console/panel, never swallowed.
- Default to no comments; only add one where the *why* isn't obvious from the code itself.
- Prefer editing existing files over creating new ones; only create new files for genuinely new responsibilities.
- Never mutate `os.environ` of the voice-copilot process itself when computing a child's environment — always build a new merged dict.

This plan covers only the core launch mechanism. The `vc` PATH-alias install
(separate `vc` binary) and the focus router / narrate-only-when-focused
checkbox are separate plans, built on top of this one.

---

### Task 1: `free_port()` helper

**Files:**
- Create: `src/voice_copilot/net.py`
- Test: `tests/unit/test_net.py`

**Interfaces:**
- Produces: `free_port(host: str = "127.0.0.1") -> int` — used by Task 4's `vc` command to auto-allocate the panel and proxy ports so multiple `vc` instances never collide.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_net.py
import socket

from voice_copilot.net import free_port


def test_free_port_returns_a_bindable_port() -> None:
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_free_port_returns_different_ports_across_calls() -> None:
    ports = {free_port() for _ in range(5)}
    assert len(ports) > 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_net.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'voice_copilot.net'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/voice_copilot/net.py
"""Small networking helpers shared by the CLI entrypoints."""

from __future__ import annotations

import socket


def free_port(host: str = "127.0.0.1") -> int:
    """Return a currently-unused TCP port on `host`.

    There's a small window between closing this probe socket and the caller
    binding the real server to the returned port where another process
    could grab it first — acceptable here since `vc` instances are
    short-lived, locally-bound dev tools, not a multi-tenant service.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_net.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/voice_copilot/net.py tests/unit/test_net.py
git commit -m "feat: add free_port helper for auto-allocating vc instance ports"
```

---

### Task 2: `resolve_cli_for_vc()` — reuse the existing CLI catalog

**Files:**
- Modify: `src/voice_copilot/proxy/cli_shims.py` (add `ResolvedCli` dataclass + `resolve_cli_for_vc()` function; add `from dataclasses import dataclass` to the existing import block)
- Test: `tests/unit/test_cli_shims_resolve.py`

**Interfaces:**
- Consumes (already in this module, unchanged): `CLI_CATALOG: dict[str, CliCatalogEntry]`, `_profile_from_config(cfg: Config, profile_id: str) -> ProxyCliProfileConfig`, `_resolve_binary_path(command: str, override: str | None, shim_dir: Path) -> str | None`, `_proxy_env_overrides(profile_id: str, profile: ProxyCliProfileConfig, *, proxy_url: str) -> dict[str, str]`, `_proxy_url_for(provider: str, *, host: str, port: int) -> str`, `_working_directory_from_config(cfg: Config, profile: ProxyCliProfileConfig | None = None) -> Path | None`, `proxy_shim_dir() -> Path`.
- Produces:
  ```python
  @dataclass(frozen=True)
  class ResolvedCli:
      profile_id: str
      label: str
      resolved_binary: str
      env_overrides: dict[str, str]
      working_directory: Path | None

  def resolve_cli_for_vc(
      name: str,
      cfg: Config,
      *,
      host: str = _DEFAULT_PROXY_HOST,
      port: int,
  ) -> ResolvedCli | None: ...
  ```
  Returns `None` when `name` matches neither the catalog nor a user-added
  `cfg.proxy_cli.profiles` entry. Raises `RuntimeError` (message: `` could
  not resolve `{command}` on PATH; set a Binary override first ``) when a
  match is found but its binary can't be located — this is Task 4's
  signal to print the error and exit, not a tier to silently degrade into.

  Matching priority: a `CLI_CATALOG` key (profile_id) match takes priority
  over a `CLI_CATALOG` `.command` match, which takes priority over a
  `cfg.proxy_cli.profiles` key match. This matters because some catalog
  entries have a `command` different from their profile_id (e.g. profile_id
  `continue` → command `cn`, profile_id `cursor` → command `cursor-agent`):
  typing either the profile_id or the real command must resolve to the same
  profile and use the *real* command for binary lookup.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_cli_shims_resolve.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli_shims_resolve.py -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_cli_for_vc'`

- [ ] **Step 3: Write minimal implementation**

Add `from dataclasses import dataclass` to the top of
`src/voice_copilot/proxy/cli_shims.py`'s import block (next to the existing
`import ctypes` etc.). Then add this directly after `launch_cli_profile`
(after its closing `}` / before `def choose_cli_working_directory`):

```python
@dataclass(frozen=True)
class ResolvedCli:
    profile_id: str
    label: str
    resolved_binary: str
    env_overrides: dict[str, str]
    working_directory: Path | None


def resolve_cli_for_vc(
    name: str,
    cfg: Config,
    *,
    host: str = _DEFAULT_PROXY_HOST,
    port: int,
) -> ResolvedCli | None:
    """Resolve a typed CLI name (`vc <name>`) to its launch profile.

    Checks the catalog by profile_id, then by its actual `command` (some
    entries differ, e.g. profile_id `continue` has command `cn`), then
    falls back to a user-added `cfg.proxy_cli.profiles` entry whose key
    isn't in the catalog at all. Returns `None` if nothing matches.
    """
    if name in CLI_CATALOG:
        profile_id = name
        meta = CLI_CATALOG[name]
        command = meta.command
        label = meta.label
    else:
        catalog_match = next(
            ((pid, meta) for pid, meta in CLI_CATALOG.items() if meta.command == name),
            None,
        )
        if catalog_match is not None:
            profile_id, meta = catalog_match
            command = meta.command
            label = meta.label
        elif name in cfg.proxy_cli.profiles:
            profile_id, command, label = name, name, name
        else:
            return None

    profile = _profile_from_config(cfg, profile_id)
    resolved_binary = _resolve_binary_path(command, profile.binary_path, proxy_shim_dir())
    if not resolved_binary:
        raise RuntimeError(f"could not resolve `{command}` on PATH; set a Binary override first")

    env_overrides = _proxy_env_overrides(
        profile_id, profile, proxy_url=_proxy_url_for(profile.provider, host=host, port=port)
    )
    working_directory = _working_directory_from_config(cfg, profile)
    return ResolvedCli(
        profile_id=profile_id,
        label=label,
        resolved_binary=resolved_binary,
        env_overrides=env_overrides,
        working_directory=working_directory,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli_shims_resolve.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `uv run pytest tests/ -v`
Expected: PASS (all tests, including the pre-existing ones)

- [ ] **Step 6: Commit**

```bash
git add src/voice_copilot/proxy/cli_shims.py tests/unit/test_cli_shims_resolve.py
git commit -m "feat: resolve a typed CLI name to its proxy profile, reusing the existing catalog"
```

---

### Task 3: `PtyAdapter` — live terminal via pywinpty / ptyprocess

**Library note (why not pexpect/wexpect):** `wexpect` (the only Windows
pexpect-alike with a built-in `.interact()`) ships a single stable release,
`4.0.0`, that crashes on import (`import pkg_resources` without declaring a
`setuptools` dependency). Rather than depend on a broken, barely-maintained
package, this task uses the maintained ConPTY binding `pywinpty`
(`winpty.PtyProcess`) on Windows and `ptyprocess.PtyProcess` on POSIX. Both
expose the same `spawn / read / write / isalive / terminate / wait /
setwinsize / fileno` surface (winpty deliberately mirrors ptyprocess's API),
so one adapter drives either. Neither has `.interact()`, so this task writes
the bidirectional terminal pump itself — the one piece `.interact()` used to
provide.

**Testability boundary (read before implementing):** the pump does raw
console/termios I/O against the real terminal. No automated test — and no
non-interactive shell — can exercise that; it is verified by a human at a
real terminal in Task 5. Therefore the unit tests here cover only the
*mockable surface* (spawn arguments, SESSION_STARTED, stdin injection,
terminate, exit-task completion) by monkeypatching the `_PtyProcess` class.
The pump method must be written so that when stdin is **not** a TTY (as under
pytest), it skips raw-mode setup and the real I/O loop — see `_pump` below.

**Files:**
- Modify: `pyproject.toml` (add PTY dependencies)
- Create: `src/voice_copilot/adapters/pty_adapter.py`
- Modify: `src/voice_copilot/adapters/__init__.py` (export `PtyAdapter`)
- Test: `tests/unit/test_pty_adapter.py`

**Interfaces:**
- Consumes: `CLIAdapter` (`adapters/base.py`) — `start()`, `send_user_message()`, `stop()`, inherited `pause()`/`resume()`/`is_paused` (overridden here since the base implementation suspends `self._proc.pid`, an `asyncio.subprocess.Process`, and this adapter has no such object — it has a `PtyProcess` child instead); `EventBus.publish()`; `Event`, `EventKind.SESSION_STARTED`, `EventKind.SESSION_ENDED` (`core/events.py`).
- Produces: `PtyAdapter(bus, argv, *, env=None, cwd=None)` with `name = "pty"`, `quick_aside = QuickAsideCapability.QUEUE`, and `exit_task() -> asyncio.Task[None] | None` — the task that completes once the wrapped process exits (i.e. once the pump loop returns because the child is no longer alive). Task 4 awaits this to shut the whole `vc` process down when the wrapped CLI exits, not just on Ctrl+C.

- [ ] **Step 1: Add PTY dependencies**

Edit `pyproject.toml`'s `dependencies` list (the one already containing
`"psutil>=5.9",`) to add, right after that line:

```toml
    "ptyprocess>=0.7 ; sys_platform != 'win32'",
    "pywinpty>=2.0 ; sys_platform == 'win32'",
```

Run: `uv sync --extra dev`
Expected: resolves and installs `pywinpty` (this machine is `win32`) without
installing `ptyprocess`. Confirm it imports: `uv run python -c "from winpty import PtyProcess; print('ok')"`.

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/test_pty_adapter.py
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
    child, spawn_calls = fake_pty
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
    assert child.written == ["hello\r"]
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_pty_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'voice_copilot.adapters.pty_adapter'`

- [ ] **Step 4: Write minimal implementation**

```python
# src/voice_copilot/adapters/pty_adapter.py
"""Live-terminal adapter for `vc <name>`.

Spawns the child in a PTY and bridges it to the user's real terminal so the
wrapped CLI's TUI renders exactly as if run directly, while still letting us
inject push-to-talk text into its stdin. Narration comes entirely from the
proxy (see `proxy/cli_shims.py`'s `resolve_cli_for_vc`) — this adapter never
parses the child's rendered output.

Windows uses pywinpty's ConPTY-backed `PtyProcess`; POSIX uses
`ptyprocess.PtyProcess`. Both expose the same spawn/read/write/isalive/
terminate surface, so a single bidirectional pump drives either. We write
that pump ourselves because the maintained ConPTY binding has no
`.interact()`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from voice_copilot.adapters.base import CLIAdapter, QuickAsideCapability
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import Event, EventKind

log = logging.getLogger(__name__)

if sys.platform == "win32":
    from winpty import PtyProcess as _PtyProcess  # type: ignore[import-untyped]
else:
    from ptyprocess import PtyProcess as _PtyProcess  # type: ignore[import-untyped]


class PtyAdapter(CLIAdapter):
    name = "pty"
    quick_aside = QuickAsideCapability.QUEUE

    def __init__(
        self,
        bus: EventBus,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._bus = bus
        self._argv = argv
        self._env = env
        self._cwd = cwd
        self._child: Any = None
        self._pump_task: asyncio.Task[None] | None = None
        self._paused = False

    async def start(self, initial_prompt: str | None = None) -> None:
        self._child = _PtyProcess.spawn(self._argv, cwd=self._cwd, env=self._env)
        await self._bus.publish(
            Event(kind=EventKind.SESSION_STARTED, source="pty", payload={"argv": self._argv})
        )
        self._pump_task = asyncio.create_task(asyncio.to_thread(self._pump), name="pty.pump")

    async def send_user_message(self, text: str, *, urgent: bool = False) -> None:
        if self._child is None or not self._child.isalive():
            return
        # PtyProcess writes a single string; a trailing CR submits the line
        # to the child just like an Enter keypress in the terminal would.
        await asyncio.to_thread(self._write, text + "\r")

    async def stop(self) -> None:
        if self._child is not None and self._child.isalive():
            try:
                self._child.terminate(force=True)
            except Exception as e:  # noqa: BLE001 - terminate is best-effort on shutdown
                log.warning("pty terminate failed: %s", e)
        if self._pump_task is not None:
            self._pump_task.cancel()
        await self._bus.publish(Event(kind=EventKind.SESSION_ENDED, source="pty"))

    async def pause(self) -> bool:
        if self._child is None or not self._child.isalive() or self._paused:
            return False
        try:
            import psutil

            psutil.Process(self._child.pid).suspend()
        except Exception as e:
            log.warning("pty pause failed: %s", e)
            return False
        self._paused = True
        return True

    async def resume(self) -> bool:
        if self._child is None or not self._paused:
            return False
        try:
            import psutil

            psutil.Process(self._child.pid).resume()
        except Exception as e:
            log.warning("pty resume failed: %s", e)
            return False
        self._paused = False
        return True

    def exit_task(self) -> asyncio.Task[None] | None:
        return self._pump_task

    def _write(self, data: str) -> None:
        child = self._child
        if child is None:
            return
        # ptyprocess.write expects bytes; winpty.PtyProcess.write expects str.
        if sys.platform == "win32":
            child.write(data)
        else:
            child.write(data.encode())

    def _pump(self) -> None:
        """Bridge the child PTY to the real terminal until the child exits.

        Runs in a worker thread. When stdin is not a TTY (under pytest, or
        when output is piped) there is no terminal to bridge: we skip
        raw-mode setup and the interactive loop and just wait for the child
        to finish, so the adapter stays importable and unit-testable without
        touching real console state.
        """
        child = self._child
        if child is None:
            return

        if not sys.stdin.isatty():
            self._drain_until_exit(child)
            return

        if sys.platform == "win32":
            self._pump_windows(child)
        else:
            self._pump_posix(child)

    def _drain_until_exit(self, child: Any) -> None:
        while True:
            try:
                child.read(1024)
            except EOFError:
                return
            except Exception:  # noqa: BLE001 - any read failure means the child is gone
                return
            if not child.isalive():
                return

    def _pump_windows(self, child: Any) -> None:
        import msvcrt
        import threading

        def feed_stdin() -> None:
            while child.isalive():
                ch = msvcrt.getwch()
                try:
                    child.write(ch)
                except Exception:  # noqa: BLE001 - child gone
                    return

        feeder = threading.Thread(target=feed_stdin, name="pty.stdin", daemon=True)
        feeder.start()
        while True:
            try:
                data = child.read(1024)
            except EOFError:
                return
            except Exception:  # noqa: BLE001 - child gone
                return
            if data:
                sys.stdout.write(data)
                sys.stdout.flush()
            if not child.isalive():
                return

    def _pump_posix(self, child: Any) -> None:
        import os
        import select
        import termios
        import tty

        stdin_fd = sys.stdin.fileno()
        old = termios.tcgetattr(stdin_fd)
        try:
            tty.setraw(stdin_fd)
            child_fd = child.fileno()
            while child.isalive():
                rlist, _, _ = select.select([child_fd, stdin_fd], [], [], 0.05)
                if child_fd in rlist:
                    try:
                        data = child.read(1024)
                    except EOFError:
                        break
                    if data:
                        os.write(sys.stdout.fileno(), data)
                if stdin_fd in rlist:
                    user = os.read(stdin_fd, 1024)
                    if user:
                        child.write(user)
        finally:
            termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old)
```

Add to `src/voice_copilot/adapters/__init__.py`:

```python
from voice_copilot.adapters.pty_adapter import PtyAdapter
```

and add `"PtyAdapter"` to its `__all__` list (alphabetically, after
`"CodexAdapter"` and before `"QuickAsideCapability"`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_pty_adapter.py -v`
Expected: PASS (4 tests). They pass because the monkeypatched `_PtyProcess`
returns a `_FakeChild`, and under pytest `sys.stdin.isatty()` is False, so
`_pump` takes the `_drain_until_exit` path — whose first `child.read()`
raises `EOFError` and returns immediately, completing the pump task.

- [ ] **Step 6: Run the full test suite, lint, and type-check**

Run: `uv run pytest tests/ -v && uv run ruff check src/voice_copilot/adapters/pty_adapter.py && uv run ruff format --check src/voice_copilot/adapters/pty_adapter.py && uv run mypy src/voice_copilot/adapters/pty_adapter.py`
Expected: all PASS. The `from winpty import PtyProcess` / `from ptyprocess
import PtyProcess` lines carry `# type: ignore[import-untyped]` already;
keep them only if `mypy` reports the import as untyped, otherwise remove.
The platform-specific `_pump_windows`/`_pump_posix` import stdlib modules
(`msvcrt` is Windows-only, `termios`/`tty` are POSIX-only) inside the
method bodies so the module imports cleanly on both platforms; `mypy` on
win32 may flag the POSIX-only modules as unreachable/missing — if so, add a
targeted `# type: ignore` on those specific import lines, not module-wide.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/voice_copilot/adapters/pty_adapter.py src/voice_copilot/adapters/__init__.py tests/unit/test_pty_adapter.py
git commit -m "feat: add PtyAdapter over pywinpty/ptyprocess with a hand-written terminal pump"
```

---

### Task 4: `vc` dispatch command + per-instance orchestration

**Files:**
- Modify: `src/voice_copilot/cli.py` (add `_normalize_argv`, `main()`, the `vc` command, `_run_vc`, `_await_vc_shutdown`; change the module-level entry point usage)
- Modify: `src/voice_copilot/__main__.py` (call `main()` instead of `app()`)
- Modify: `pyproject.toml` (`[project.scripts]` entry point)
- Test: `tests/unit/test_cli_dispatch.py`

**Interfaces:**
- Consumes: `resolve_cli_for_vc`, `ResolvedCli` (Task 2, `proxy/cli_shims.py`); `PtyAdapter` (Task 3, `adapters/pty_adapter.py`); `free_port` (Task 1, `net.py`); existing `_boot`, `_server_app_state`, `_start_servers`, `_start_tts_driver`, `Commentator`, `DialogManager`, `SessionRegistry`, `build_proxy_server`, `EventBus`, `load_config` (all already imported in `cli.py`).
- Produces: `_normalize_argv(argv: list[str]) -> list[str]` (pure, unit
  tested directly); `main() -> None` (new console-script entry point); the
  `vc` Typer command, callable as `voice-copilot vc <name>` or, via argv
  rewriting, plain `voice-copilot <name>`.

- [ ] **Step 1: Write the failing dispatch test**

```python
# tests/unit/test_cli_dispatch.py
from voice_copilot.cli import _normalize_argv


def test_unknown_name_gets_vc_inserted() -> None:
    assert _normalize_argv(["voice-copilot", "claude"]) == ["voice-copilot", "vc", "claude"]


def test_known_subcommand_passes_through() -> None:
    assert _normalize_argv(["voice-copilot", "serve"]) == ["voice-copilot", "serve"]


def test_explicit_vc_passes_through() -> None:
    assert _normalize_argv(["voice-copilot", "vc", "claude"]) == ["voice-copilot", "vc", "claude"]


def test_flag_passes_through() -> None:
    assert _normalize_argv(["voice-copilot", "--help"]) == ["voice-copilot", "--help"]


def test_bare_invocation_passes_through() -> None:
    assert _normalize_argv(["voice-copilot"]) == ["voice-copilot"]


def test_extra_args_after_name_are_preserved() -> None:
    assert _normalize_argv(["voice-copilot", "claude", "--", "-p", "hi"]) == [
        "voice-copilot",
        "vc",
        "claude",
        "--",
        "-p",
        "hi",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_cli_dispatch.py -v`
Expected: FAIL with `ImportError: cannot import name '_normalize_argv'`

- [ ] **Step 3: Implement dispatch + orchestration**

Add these imports to the top of `src/voice_copilot/cli.py`, alongside the
existing `from voice_copilot.adapters import ClaudeCodeAdapter, CodexAdapter`
line:

```python
from voice_copilot.adapters import ClaudeCodeAdapter, CodexAdapter, PtyAdapter
```

and alongside the existing `from voice_copilot.proxy.server import
base_urls_for, build_proxy_server` line:

```python
from voice_copilot.net import free_port
from voice_copilot.proxy.cli_shims import ResolvedCli, resolve_cli_for_vc
```

Add `import sys` to the existing `import` block at the top (next to
`import asyncio`, `import logging`, `import os`).

Add this near the top of the file, right after the `app = typer.Typer(...)`
/ `console = Console()` block:

```python
_KNOWN_SUBCOMMANDS = {"version", "serve", "run", "proxy", "config", "vc"}


def _normalize_argv(argv: list[str]) -> list[str]:
    """Let `voice-copilot <name>` work without typing `vc` first.

    Rewrites to `voice-copilot vc <name> ...` whenever the first argument
    isn't a flag or one of our own subcommands.
    """
    if len(argv) < 2:
        return argv
    first = argv[1]
    if first.startswith("-") or first in _KNOWN_SUBCOMMANDS:
        return argv
    return [argv[0], "vc", *argv[1:]]


def main() -> None:
    sys.argv[:] = _normalize_argv(sys.argv)
    app()
```

Add the `vc` command. Place it after the existing `config()` command and
before `_start_tts_driver`:

```python
@app.command(name="vc")
def vc_launch(
    name: str = typer.Argument(..., help="CLI to launch and narrate, e.g. claude, codex, opencode."),
    cli_args: list[str] = typer.Argument(None, help="Arguments forwarded to the target CLI, after `--`."),
    host: str = typer.Option("127.0.0.1", envvar="VOICE_COPILOT_HOST"),
    port: int = typer.Option(0, "--port", help="Panel port. 0 picks a free port automatically."),
    proxy_port: int = typer.Option(0, "--proxy-port", help="Proxy port. 0 picks a free port automatically."),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
    hotkeys: bool = typer.Option(True, "--hotkeys/--no-hotkeys"),
    tray: bool = typer.Option(True, "--tray/--no-tray"),
) -> None:
    """Launch NAME in a live terminal, auto-narrating via the proxy when NAME is known."""
    asyncio.run(
        _run_vc(
            name=name,
            cli_args=cli_args or [],
            host=host,
            port=port or free_port(host),
            proxy_port=proxy_port,
            open_browser=open_browser,
            enable_hotkeys=hotkeys,
            enable_tray=tray,
        )
    )
```

Add `_await_vc_shutdown` right after the existing `_await_shutdown`
function:

```python
async def _await_vc_shutdown(
    servers: list[uvicorn.Server],
    server_tasks: list[asyncio.Task[Any]],
    extra_tasks: list[asyncio.Task[Any]],
    child_exit_task: asyncio.Task[None] | None,
    *,
    hotkey_svc: HotkeyService | None = None,
    tray_svc: TrayService | None = None,
    cleanup: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Like `_await_shutdown`, but also returns once the wrapped CLI exits
    on its own. `vc` wraps one foreground terminal session, not a
    standalone server — once that session ends there's nothing left to
    wrap, so the whole process should exit instead of waiting for Ctrl+C.
    """
    wait_tasks = [*server_tasks, *extra_tasks]
    if child_exit_task is not None:
        wait_tasks.append(child_exit_task)
    try:
        await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for s in servers:
            s.should_exit = True
        for t in extra_tasks:
            t.cancel()
        await asyncio.gather(*server_tasks, *extra_tasks, return_exceptions=True)
        if cleanup is not None:
            await cleanup()
        if hotkey_svc is not None:
            hotkey_svc.stop()
        if tray_svc is not None:
            tray_svc.stop()
```

Add `_run_vc` right after `_run_with_adapter`:

```python
async def _run_vc(
    name: str,
    cli_args: list[str],
    host: str,
    port: int,
    proxy_port: int,
    open_browser: bool,
    enable_hotkeys: bool,
    enable_tray: bool,
) -> None:
    bus = EventBus()
    cfg_for_resolve = load_config()
    actual_proxy_port = proxy_port or free_port(host)

    resolved: ResolvedCli | None
    try:
        resolved = resolve_cli_for_vc(name, cfg_for_resolve, host=host, port=actual_proxy_port)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        return

    enable_proxy = resolved is not None
    sessions = SessionRegistry() if enable_proxy else None
    server, hotkey_svc, tray_svc, cfg, hub = await _boot(
        bus,
        host,
        port,
        open_browser,
        enable_hotkeys,
        enable_tray,
        sessions=sessions,
        proxy_port=actual_proxy_port if enable_proxy else None,
    )

    commentator = Commentator(bus, cfg.commentator, cfg.commentator_language, sessions=sessions)
    _server_app_state(server).commentator = commentator
    servers: list[uvicorn.Server] = [server]
    if enable_proxy:
        servers.append(build_proxy_server(bus, host=host, port=actual_proxy_port, registry=sessions))
    server_tasks = _start_servers(servers)
    extra: list[asyncio.Task[Any]] = [asyncio.create_task(commentator.run(), name="commentator")]
    tts_task = _start_tts_driver(bus, hub, cfg)
    if tts_task is not None:
        extra.append(tts_task)

    if resolved is not None:
        binary = resolved.resolved_binary
        full_env = {**os.environ, **resolved.env_overrides}
        cwd = str(resolved.working_directory) if resolved.working_directory else None
        console.print(f"[green]{resolved.label}: narrating via {resolved.env_overrides}[/green]")
        await asyncio.sleep(0.3)
    else:
        binary = name
        full_env = dict(os.environ)
        cwd = None
        console.print(
            f"[yellow]`{name}` isn't a recognized CLI — launching without narration. "
            f"Add a `proxy_cli.profiles.{name}` entry to your config to enable it.[/yellow]"
        )

    adapter = PtyAdapter(bus, [binary, *cli_args], env=full_env, cwd=cwd)
    dialog = DialogManager(bus, adapter, cfg.dialog)
    extra.append(asyncio.create_task(dialog.run(), name="dialog"))

    try:
        await adapter.start()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        for s in servers:
            s.should_exit = True
        for t in extra:
            t.cancel()
        await asyncio.gather(*server_tasks, *extra, return_exceptions=True)
        if hotkey_svc is not None:
            hotkey_svc.stop()
        if tray_svc is not None:
            tray_svc.stop()
        return

    await _await_vc_shutdown(
        servers,
        server_tasks,
        extra,
        adapter.exit_task(),
        hotkey_svc=hotkey_svc,
        tray_svc=tray_svc,
        cleanup=adapter.stop,
    )
```

Update `src/voice_copilot/__main__.py`:

```python
from voice_copilot.cli import main

if __name__ == "__main__":
    main()
```

Update `pyproject.toml`'s `[project.scripts]`:

```toml
[project.scripts]
voice-copilot = "voice_copilot.cli:main"
```

- [ ] **Step 4: Run dispatch test to verify it passes**

Run: `uv run pytest tests/unit/test_cli_dispatch.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full test suite, lint, and type-check**

Run: `uv run pytest tests/ -v && uv run ruff check src/voice_copilot/cli.py src/voice_copilot/__main__.py && uv run ruff format --check src/voice_copilot/cli.py src/voice_copilot/__main__.py && uv run mypy src/voice_copilot/cli.py`
Expected: all PASS.

- [ ] **Step 6: Reinstall the entry point and smoke-test dispatch**

Run: `uv sync` (picks up the changed `[project.scripts]` entry point), then:
`uv run voice-copilot version` (expect normal version output — proves
`main()` still dispatches to existing subcommands correctly).

- [ ] **Step 7: Commit**

```bash
git add src/voice_copilot/cli.py src/voice_copilot/__main__.py pyproject.toml tests/unit/test_cli_dispatch.py
git commit -m "feat: add vc launch command with argv dispatch and exit-on-child-exit"
```

---

### Task 5: Manual end-to-end verification

This task has no automated test — `.interact()` takes over the real
terminal, which can't be driven from pytest. Do not consider this plan
done until this task passes for real, per verification-before-completion:
automated tests only proved the pure logic (resolution, dispatch
rewriting) and the mocked adapter calls — they did not prove `wexpect`'s
actual `.interact()` behaves as expected on this machine, or that real
CLIs honor the env override in practice.

- [ ] **Step 1: Smoke-test with a trivial command**

Run: `uv run voice-copilot vc cmd /c echo hello` (Windows) — should print
`hello`, then the `vc` process should exit on its own (not hang waiting for
Ctrl+C), proving `_await_vc_shutdown`'s exit-on-child-exit path works.

- [ ] **Step 2: Verify push-to-talk injection reaches a real interactive child**

Run: `uv run voice-copilot vc cmd` (a plain interactive shell) — confirm
you can type normally into it (proving the real terminal was correctly
handed over via `.interact()`), then trigger push-to-talk
(`alt+space` by default) and speak a short phrase — confirm the
transcribed text actually appears as input to the `cmd` session.

- [ ] **Step 3: Verify auto-narration end to end with a real catalog CLI**

If `claude` (or `codex`) is installed and authenticated on this machine:
run `uv run voice-copilot vc claude`. Confirm: the browser panel opens, the
panel shows narration once you give Claude something to do, and the
terminal shows Claude's normal interactive TUI exactly as `claude` run
directly would.

- [ ] **Step 4: Verify the unknown-CLI path**

Run: `uv run voice-copilot vc cmd /c echo no-narration-test` — confirm the
console prints the "isn't a recognized CLI" message and the command still
runs to completion.

- [ ] **Step 5: Note results**

Record pass/fail for each of the above in the PR description or commit
message for this task — do not mark this plan complete if any step failed.
