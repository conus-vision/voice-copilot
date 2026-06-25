# Focus Router + Narrate-Only-When-Focused Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When several `vc` instances run at once, exactly one of them should ever be speaking (TTS) at a time, and the OS-level push-to-talk hotkey should only act in whichever instance currently has the user's attention — gated by a new "Narrate only when focused" checkbox in Settings (checked = strict live focus, unchecked = sticky to whichever instance was most recently focused, across all running `vc` processes).

**Architecture:** Each `vc` process gets its own `FocusRouter`, polling whether *its own* terminal (via a Windows `GetForegroundWindow`/`GetConsoleWindow` check) or *its own* browser panel (reported by the browser itself via `document.hasFocus()`/`blur` over the existing WebSocket) currently has focus. The router gates two existing mechanisms it doesn't own: `TTSDriver.set_focus_gate()` (new, parallel to the existing manual `set_muted()`) for output, and a new `is_focused` predicate on `HotkeyService` for input. For the sticky (unchecked) case across *multiple processes*, each router writes "I was just focused" to a small shared JSON file in the config directory; an instance only narrates in sticky mode if it wrote the most recent claim.

**Tech Stack:** Python 3.11+ standard library only (`ctypes` for the Windows focus check, already an established pattern in this codebase via `proxy/cli_shims.py`'s `_broadcast_environment_change`). No new dependencies. Small, additive frontend JS/HTML changes — no new frontend tooling.

## Global Constraints

- Python 3.11+, formatted with `ruff format`, linted with `ruff`, `mypy` strict on new code.
- No hidden retries, no silent fallbacks — except the one documented exception below (alias-style "nothing to report" silence is *not* used here; every gating decision is observable via `muted`/`current_focus`).
- Prefer editing existing files over creating new ones.
- Terminal-focus detection is Windows-only by design (no cross-desktop POSIX equivalent of `GetForegroundWindow`/`GetConsoleWindow` exists) — `is_console_window_focused()` always returns `False` on non-Windows, meaning on POSIX only the browser-panel-focus signal gates narration/hotkeys. This is a known, accepted platform gap, not an oversight.

This plan builds on `docs/superpowers/plans/2026-06-25-vc-launch-core.md` (needs `_run_vc`, `_boot`, `_start_tts_driver` to already exist in `cli.py`).

---

### Task 1: `FocusConfig` setting

**Files:**
- Modify: `src/voice_copilot/core/config.py`
- Modify: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `FocusConfig.narrate_only_when_focused: bool = True`, exposed as `Config.focus`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_config.py`:

```python
def test_default_config_has_narrate_only_when_focused_enabled(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.focus.narrate_only_when_focused is True


def test_narrate_only_when_focused_round_trips_through_save_and_load(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    cfg = load_config(config_file)
    cfg.focus.narrate_only_when_focused = False
    save_config(cfg, config_file)
    reloaded = load_config(config_file)
    assert reloaded.focus.narrate_only_when_focused is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'focus'`

- [ ] **Step 3: Write minimal implementation**

In `src/voice_copilot/core/config.py`, add this class right after `DialogConfig`:

```python
class FocusConfig(BaseModel):
    narrate_only_when_focused: bool = True
```

Add a field to `Config`, right after `dialog: DialogConfig = DialogConfig()`:

```python
    focus: FocusConfig = FocusConfig()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add src/voice_copilot/core/config.py tests/unit/test_config.py
git commit -m "feat: add narrate_only_when_focused config setting"
```

---

### Task 2: `FocusRouter` + cross-process sticky state

**Files:**
- Create: `src/voice_copilot/focus.py`
- Test: `tests/unit/test_focus.py`

**Interfaces:**
- Produces:
  ```python
  def is_console_window_focused() -> bool: ...
  def shared_focus_state_path() -> Path: ...
  def record_focus(pid: int | None = None) -> None: ...
  def is_last_focused(pid: int | None = None) -> bool: ...

  class FocusRouter:
      def __init__(self, *, narrate_only_when_focused: bool) -> None: ...
      def set_panel_focus(self, focused: bool) -> None: ...
      def on_narrate_gate(self, callback: Callable[[bool], None]) -> None: ...
      @property
      def current_focus(self) -> bool: ...
      def start(self) -> None: ...
      def stop(self) -> None: ...
  ```
  `current_focus` is what Task 4's `HotkeyService.is_focused` predicate reads.
  `on_narrate_gate`'s callback is what Task 3's `TTSDriver.set_focus_gate` gets
  wired to (Task 5).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_focus.py
import pytest

from voice_copilot.focus import FocusRouter, is_last_focused, record_focus


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "voice_copilot.focus.shared_focus_state_path", lambda: tmp_path / "focus-state.json"
    )
    return tmp_path


def test_is_last_focused_false_when_no_state_recorded(isolated_state) -> None:
    assert is_last_focused(pid=1234) is False


def test_record_then_is_last_focused_for_same_pid(isolated_state) -> None:
    record_focus(pid=1234)
    assert is_last_focused(pid=1234) is True
    assert is_last_focused(pid=5678) is False


def test_later_record_supersedes_earlier_one(isolated_state) -> None:
    record_focus(pid=1111)
    record_focus(pid=2222)
    assert is_last_focused(pid=1111) is False
    assert is_last_focused(pid=2222) is True


@pytest.mark.asyncio
async def test_tick_sets_current_focus_from_terminal(monkeypatch, isolated_state) -> None:
    monkeypatch.setattr("voice_copilot.focus.is_console_window_focused", lambda: True)
    router = FocusRouter(narrate_only_when_focused=True)
    await router._tick()
    assert router.current_focus is True


@pytest.mark.asyncio
async def test_tick_sets_current_focus_from_panel(monkeypatch, isolated_state) -> None:
    monkeypatch.setattr("voice_copilot.focus.is_console_window_focused", lambda: False)
    router = FocusRouter(narrate_only_when_focused=True)
    router.set_panel_focus(True)
    await router._tick()
    assert router.current_focus is True


def test_should_narrate_uses_current_focus_when_checked() -> None:
    router = FocusRouter(narrate_only_when_focused=True)
    assert router._should_narrate() is False
    router._current = True
    assert router._should_narrate() is True


def test_should_narrate_uses_sticky_state_when_unchecked(isolated_state) -> None:
    router = FocusRouter(narrate_only_when_focused=False)
    assert router._should_narrate() is False
    record_focus()
    assert router._should_narrate() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_focus.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'voice_copilot.focus'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/voice_copilot/focus.py
"""Per-instance focus tracking, used to gate narration and push-to-talk so
multiple simultaneously running `vc` instances don't act on the same event
or speak over each other.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from voice_copilot.core.config import config_path

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.5


def is_console_window_focused() -> bool:
    """True if THIS process's own console window is the OS foreground window.

    Always False on non-Windows — there's no single, desktop-environment-
    independent POSIX equivalent of GetForegroundWindow/GetConsoleWindow.
    """
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    return bool(user32.GetForegroundWindow() == kernel32.GetConsoleWindow())


def shared_focus_state_path() -> Path:
    return config_path().parent / "focus-state.json"


def record_focus(pid: int | None = None) -> None:
    """Claim "most recently focused" system-wide — used for sticky
    narration when `narrate_only_when_focused` is off and several `vc`
    instances are running at once."""
    path = shared_focus_state_path()
    payload = {"pid": pid if pid is not None else os.getpid(), "ts": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.debug("could not record focus state: %s", e)


def is_last_focused(pid: int | None = None) -> bool:
    path = shared_focus_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("pid") == (pid if pid is not None else os.getpid())


class FocusRouter:
    def __init__(self, *, narrate_only_when_focused: bool) -> None:
        self._narrate_only_when_focused = narrate_only_when_focused
        self._panel_focused = False
        self._current = False
        self._task: asyncio.Task[None] | None = None
        self._on_narrate_gate: Callable[[bool], None] | None = None

    def set_panel_focus(self, focused: bool) -> None:
        self._panel_focused = focused

    def on_narrate_gate(self, callback: Callable[[bool], None]) -> None:
        self._on_narrate_gate = callback

    @property
    def current_focus(self) -> bool:
        return self._current

    def start(self) -> None:
        self._task = asyncio.create_task(self._poll(), name="focus-router")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _tick(self) -> None:
        terminal_focused = await asyncio.to_thread(is_console_window_focused)
        self._current = terminal_focused or self._panel_focused
        if self._current:
            record_focus()

    def _should_narrate(self) -> bool:
        if self._narrate_only_when_focused:
            return self._current
        return is_last_focused()

    async def _poll(self) -> None:
        last: bool | None = None
        while True:
            await self._tick()
            should = self._should_narrate()
            if should != last:
                last = should
                if self._on_narrate_gate is not None:
                    self._on_narrate_gate(should)
            await asyncio.sleep(_POLL_INTERVAL_S)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_focus.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full test suite, lint, and type-check**

Run: `uv run pytest tests/ -v && uv run ruff check src/voice_copilot/focus.py && uv run ruff format --check src/voice_copilot/focus.py && uv run mypy src/voice_copilot/focus.py`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/voice_copilot/focus.py tests/unit/test_focus.py
git commit -m "feat: add FocusRouter with cross-process sticky focus state"
```

---

### Task 3: `TTSDriver.set_focus_gate()`

**Files:**
- Modify: `src/voice_copilot/audio/tts_driver.py`
- Test: `tests/unit/test_tts_driver_focus_gate.py`

**Interfaces:**
- Consumes: existing `TTSDriver.__init__`, `_clear_pending`, `_abort_current`, `_clear_abort_task` (unchanged).
- Produces: `TTSDriver.muted` (new public property, replacing internal-only
  `self._muted`), `TTSDriver.set_focus_gate(allowed: bool) -> None` (new
  method, parallel to the existing `set_muted`). `Task 5` wires
  `FocusRouter.on_narrate_gate` to call this.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_tts_driver_focus_gate.py
from voice_copilot.audio.hub import AudioHub
from voice_copilot.audio.tts_driver import TTSDriver
from voice_copilot.core.bus import EventBus


class _FakeTTS:
    output_format = "mp3"

    async def synthesize(self, text: str, *, language: str):
        return
        yield  # pragma: no cover - never reached, makes this an async generator


def _make_driver() -> TTSDriver:
    return TTSDriver(EventBus(), AudioHub(), _FakeTTS(), "en")


def test_muted_is_false_by_default() -> None:
    assert _make_driver().muted is False


def test_focus_gate_closing_mutes() -> None:
    driver = _make_driver()
    driver.set_focus_gate(False)
    assert driver.muted is True
    driver.set_focus_gate(True)
    assert driver.muted is False


def test_manual_mute_holds_even_if_focus_gate_reopens() -> None:
    driver = _make_driver()
    driver.set_muted(True)
    driver.set_focus_gate(False)
    driver.set_focus_gate(True)
    assert driver.muted is True  # manual mute alone still holds it muted
    driver.set_muted(False)
    assert driver.muted is False


def test_focus_gate_is_a_noop_when_value_is_unchanged() -> None:
    driver = _make_driver()
    driver.set_focus_gate(True)  # already True by default — must not raise
    assert driver.muted is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_tts_driver_focus_gate.py -v`
Expected: FAIL with `AttributeError: 'TTSDriver' object has no attribute 'muted'`

- [ ] **Step 3: Write minimal implementation**

In `src/voice_copilot/audio/tts_driver.py`, in `TTSDriver.__init__`, replace:

```python
        self._muted = muted
```

with:

```python
        self._manual_muted = muted
        self._focus_allowed = True
```

Replace the existing `set_muted` method:

```python
    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        if muted:
            self._clear_pending()
            # Fire-and-forget abort — don't block the caller.
            task = asyncio.create_task(self._abort_current(), name="tts.abort")
            self._abort_task = task
            task.add_done_callback(self._clear_abort_task)
```

with:

```python
    @property
    def muted(self) -> bool:
        return self._manual_muted or not self._focus_allowed

    def set_muted(self, muted: bool) -> None:
        self._manual_muted = muted
        if muted:
            self._clear_pending()
            # Fire-and-forget abort — don't block the caller.
            task = asyncio.create_task(self._abort_current(), name="tts.abort")
            self._abort_task = task
            task.add_done_callback(self._clear_abort_task)

    def set_focus_gate(self, allowed: bool) -> None:
        """Narrate-only-when-focused gate — independent of the manual mute
        toggle above; effectively muted whenever either says no."""
        if allowed == self._focus_allowed:
            return
        self._focus_allowed = allowed
        if not allowed:
            self._clear_pending()
            task = asyncio.create_task(self._abort_current(), name="tts.focus-abort")
            self._abort_task = task
            task.add_done_callback(self._clear_abort_task)
```

Then update the two other usages of `self._muted` to `self.muted`:
in `_build_utterance`, `if self._muted or self._is_stale_utterance(ev):` →
`if self.muted or self._is_stale_utterance(ev):`; in `_speaker_loop`,
`if self._muted:` → `if self.muted:`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_tts_driver_focus_gate.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full test suite, lint, and type-check**

Run: `uv run pytest tests/ -v && uv run ruff check src/voice_copilot/audio/tts_driver.py && uv run ruff format --check src/voice_copilot/audio/tts_driver.py && uv run mypy src/voice_copilot/audio/tts_driver.py`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/voice_copilot/audio/tts_driver.py tests/unit/test_tts_driver_focus_gate.py
git commit -m "feat: add TTSDriver.set_focus_gate, independent of manual mute"
```

---

### Task 4: Focus-gated push-to-talk in `HotkeyService`

**Files:**
- Modify: `src/voice_copilot/hotkeys.py`
- Test: `tests/unit/test_hotkeys_focus_gate.py`

**Interfaces:**
- Produces: `HotkeyService.__init__(..., *, is_focused: Callable[[], bool] | None = None)`. When provided and it returns `False`, the `push_to_talk` binding specifically is skipped on press (and, since it's never added to `_active`, the matching release is skipped too, automatically, via the existing `if binding.name not in self._active: continue` guard). No other binding is affected.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_hotkeys_focus_gate.py
import asyncio

import pytest
from pynput import keyboard

from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import EventKind
from voice_copilot.hotkeys import Binding, HotkeyService


@pytest.mark.asyncio
async def test_push_to_talk_is_skipped_when_not_focused() -> None:
    bus = EventBus()
    loop = asyncio.get_running_loop()
    bindings = [
        Binding(
            name="push_to_talk",
            combo="alt+space",
            press_kind=EventKind.USER_SPEAK_REQUESTED,
            press_payload={"phase": "start"},
        )
    ]
    svc = HotkeyService(bus, loop, bindings, is_focused=lambda: False)

    async with bus.subscribe() as q:
        svc._on_press(keyboard.Key.alt)
        svc._on_press(keyboard.Key.space)
        await asyncio.sleep(0.05)
        assert q.empty()


@pytest.mark.asyncio
async def test_push_to_talk_fires_when_focused() -> None:
    bus = EventBus()
    loop = asyncio.get_running_loop()
    bindings = [
        Binding(
            name="push_to_talk",
            combo="alt+space",
            press_kind=EventKind.USER_SPEAK_REQUESTED,
            press_payload={"phase": "start"},
        )
    ]
    svc = HotkeyService(bus, loop, bindings, is_focused=lambda: True)

    async with bus.subscribe() as q:
        svc._on_press(keyboard.Key.alt)
        svc._on_press(keyboard.Key.space)
        event = await asyncio.wait_for(q.get(), timeout=1)
        assert event.kind == EventKind.USER_SPEAK_REQUESTED


@pytest.mark.asyncio
async def test_other_bindings_are_unaffected_by_focus_gate() -> None:
    bus = EventBus()
    loop = asyncio.get_running_loop()
    bindings = [
        Binding(name="interrupt", combo="alt+shift+space", press_kind=EventKind.USER_INTERRUPT)
    ]
    svc = HotkeyService(bus, loop, bindings, is_focused=lambda: False)

    async with bus.subscribe() as q:
        svc._on_press(keyboard.Key.alt)
        svc._on_press(keyboard.Key.shift)
        svc._on_press(keyboard.Key.space)
        event = await asyncio.wait_for(q.get(), timeout=1)
        assert event.kind == EventKind.USER_INTERRUPT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_hotkeys_focus_gate.py -v`
Expected: FAIL with `TypeError: HotkeyService.__init__() got an unexpected keyword argument 'is_focused'`

- [ ] **Step 3: Write minimal implementation**

In `src/voice_copilot/hotkeys.py`, add to the imports:

```python
from collections.abc import Callable
```

Change `HotkeyService.__init__`'s signature and body:

```python
    def __init__(
        self,
        bus: EventBus,
        loop: asyncio.AbstractEventLoop,
        bindings: list[Binding],
        *,
        is_focused: Callable[[], bool] | None = None,
    ) -> None:
        self._bus = bus
        self._loop = loop
        self._is_focused = is_focused
        self._bindings: list[tuple[Binding, frozenset[str], Any]] = []
```

(the rest of `__init__` is unchanged — only the new `self._is_focused = is_focused` line is inserted).

In `_on_press`, add the focus check right before `self._active.add(binding.name)`:

```python
    def _on_press(self, k: Any) -> None:
        self._pressed.add(_canonical_key(k))
        for binding, mods, key in self._bindings:
            if binding.name in self._active:
                continue
            if not self._satisfied(mods, key):
                continue
            if (
                binding.name == "push_to_talk"
                and self._is_focused is not None
                and not self._is_focused()
            ):
                continue
            self._active.add(binding.name)
            if binding.press_kind is not None:
                payload = {"hotkey": binding.combo, "name": binding.name}
                if binding.press_payload:
                    payload.update(binding.press_payload)
                self._publish(binding.press_kind, payload)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_hotkeys_focus_gate.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full test suite, lint, and type-check**

Run: `uv run pytest tests/ -v && uv run ruff check src/voice_copilot/hotkeys.py && uv run ruff format --check src/voice_copilot/hotkeys.py && uv run mypy src/voice_copilot/hotkeys.py`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/voice_copilot/hotkeys.py tests/unit/test_hotkeys_focus_gate.py
git commit -m "feat: gate push-to-talk hotkey on focus, leave other bindings unaffected"
```

---

### Task 5: `panel_focus` WebSocket command

**Files:**
- Modify: `src/voice_copilot/web/ws.py`
- Test: `tests/unit/test_ws_handle_cmd.py`

**Interfaces:**
- Consumes: `FocusRouter.set_panel_focus` (Task 2).
- Produces: a new `_handle_cmd` parameter `focus_router: FocusRouter | None`,
  and a new client→server cmd `{"type":"cmd","cmd":"panel_focus","focused":bool}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ws_handle_cmd.py
import pytest

from voice_copilot.audio.mic import MicSession
from voice_copilot.core.bus import EventBus
from voice_copilot.focus import FocusRouter
from voice_copilot.web.ws import _handle_cmd


@pytest.mark.asyncio
async def test_panel_focus_cmd_updates_focus_router() -> None:
    bus = EventBus()
    mic = MicSession()
    router = FocusRouter(narrate_only_when_focused=True)

    await _handle_cmd(
        bus, mic, {"type": "cmd", "cmd": "panel_focus", "focused": True}, None, None, router
    )
    assert router._panel_focused is True

    await _handle_cmd(
        bus, mic, {"type": "cmd", "cmd": "panel_focus", "focused": False}, None, None, router
    )
    assert router._panel_focused is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_ws_handle_cmd.py -v`
Expected: FAIL with `TypeError: _handle_cmd() takes 4 positional arguments but 6 were given`

- [ ] **Step 3: Write minimal implementation**

In `src/voice_copilot/web/ws.py`, add to the module docstring's
"Client → server" list (next to the existing `mic_end` line):

```
      { "type":"cmd", "cmd":"panel_focus", "focused":true|false }
```

Add the import:

```python
from voice_copilot.focus import FocusRouter
```

Change `_handle_cmd`'s signature:

```python
async def _handle_cmd(
    bus: EventBus,
    mic: MicSession,
    data: dict[str, Any],
    stt: STTProvider | None,
    language: str | None,
    focus_router: FocusRouter | None = None,
) -> None:
```

Add, right after the existing `cmd == "mic_end"` block (before the closing
of the function):

```python
    if cmd == "panel_focus":
        focused = data.get("focused")
        if isinstance(focused, bool) and focus_router is not None:
            focus_router.set_panel_focus(focused)
        return
```

In `register_ws`'s `ws_endpoint`, add next to the existing
`stt = getattr(ws.app.state, "stt_provider", None)` line:

```python
        focus_router: FocusRouter | None = getattr(ws.app.state, "focus_router", None)
```

and change the existing call site:

```python
                        await _handle_cmd(bus, mic, data, stt, language)
```

to:

```python
                        await _handle_cmd(bus, mic, data, stt, language, focus_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_ws_handle_cmd.py -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite, lint, and type-check**

Run: `uv run pytest tests/ -v && uv run ruff check src/voice_copilot/web/ws.py && uv run ruff format --check src/voice_copilot/web/ws.py && uv run mypy src/voice_copilot/web/ws.py`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/voice_copilot/web/ws.py tests/unit/test_ws_handle_cmd.py
git commit -m "feat: add panel_focus WS command, updating the per-instance FocusRouter"
```

---

### Task 6: Wire `FocusRouter` into `_run_vc`

**Files:**
- Modify: `src/voice_copilot/cli.py`

**Interfaces:**
- Consumes: `FocusRouter` (Task 2), `TTSDriver.set_focus_gate` (Task 3),
  `HotkeyService(..., is_focused=...)` (Task 4).
- Produces: `_start_tts_driver` now returns
  `tuple[TTSDriver, asyncio.Task[None]] | None` instead of
  `asyncio.Task[None] | None` — update **all three** existing call sites
  (`_serve`, `_proxy_only`, `_run_with_adapter`), not just the new one in
  `_run_vc`. `_boot` gains an `is_focused: Callable[[], bool] | None = None`
  parameter, forwarded to `HotkeyService`.

- [ ] **Step 1: Update `_start_tts_driver` and all call sites**

In `src/voice_copilot/cli.py`, change:

```python
def _start_tts_driver(bus: EventBus, hub: AudioHub, cfg: Config) -> asyncio.Task[None] | None:
    try:
        tts = provider_registry.build("tts", cfg.tts.name, dict(cfg.tts.options))
    except Exception as e:
        console.print(f"[yellow]TTS provider unavailable: {e}[/yellow]")
        return None
    driver = TTSDriver(bus, hub, tts, cfg.commentator_language)
    return asyncio.create_task(driver.run(), name="tts.driver")
```

to:

```python
def _start_tts_driver(
    bus: EventBus, hub: AudioHub, cfg: Config
) -> tuple[TTSDriver, asyncio.Task[None]] | None:
    try:
        tts = provider_registry.build("tts", cfg.tts.name, dict(cfg.tts.options))
    except Exception as e:
        console.print(f"[yellow]TTS provider unavailable: {e}[/yellow]")
        return None
    driver = TTSDriver(bus, hub, tts, cfg.commentator_language)
    return driver, asyncio.create_task(driver.run(), name="tts.driver")
```

In `_serve`, `_proxy_only`, and `_run_with_adapter`, each currently has:

```python
    tts_task = _start_tts_driver(bus, hub, cfg)
    if tts_task is not None:
        extra.append(tts_task)
```

Change all three occurrences to:

```python
    tts_result = _start_tts_driver(bus, hub, cfg)
    if tts_result is not None:
        extra.append(tts_result[1])
```

`TTSDriver` is already imported in `cli.py` via the existing
`from voice_copilot.audio import AudioHub, TTSDriver` line — no import
change needed for it. Add this new import:

```python
from voice_copilot.focus import FocusRouter
```

- [ ] **Step 2: Add `is_focused` to `_boot`**

Change `_boot`'s signature:

```python
async def _boot(
    bus: EventBus,
    host: str,
    port: int,
    open_browser: bool,
    enable_hotkeys: bool,
    enable_tray: bool,
    sessions: SessionRegistry | None = None,
    proxy_port: int | None = None,
    is_focused: Callable[[], bool] | None = None,
) -> tuple[uvicorn.Server, HotkeyService | None, TrayService | None, Config, AudioHub]:
```

and inside it, change:

```python
    if enable_hotkeys:
        try:
            hotkey_svc = HotkeyService(bus, loop, default_bindings(cfg.hotkeys))
            hotkey_svc.start()
```

to:

```python
    if enable_hotkeys:
        try:
            hotkey_svc = HotkeyService(
                bus, loop, default_bindings(cfg.hotkeys), is_focused=is_focused
            )
            hotkey_svc.start()
```

- [ ] **Step 3: Wire it all together in `_run_vc`**

In `_run_vc`, right after `cfg_for_resolve = load_config()`, add:

```python
    focus_router = FocusRouter(
        narrate_only_when_focused=cfg_for_resolve.focus.narrate_only_when_focused
    )
```

Change the `_boot(...)` call inside `_run_vc` to pass
`is_focused=lambda: focus_router.current_focus` as an additional keyword
argument.

Right after `_server_app_state(server).commentator = commentator`, add:

```python
    _server_app_state(server).focus_router = focus_router
```

Change the existing:

```python
    tts_task = _start_tts_driver(bus, hub, cfg)
    if tts_task is not None:
        extra.append(tts_task)
```

(inside `_run_vc` specifically) to:

```python
    tts_result = _start_tts_driver(bus, hub, cfg)
    if tts_result is not None:
        tts_driver, tts_task = tts_result
        extra.append(tts_task)
        focus_router.on_narrate_gate(tts_driver.set_focus_gate)

    focus_router.start()
```

Finally, right after the existing
`await _await_vc_shutdown(...)` call at the end of `_run_vc`, add:

```python
    focus_router.stop()
```

- [ ] **Step 4: Run the full test suite, lint, and type-check**

Run: `uv run pytest tests/ -v && uv run ruff check src/voice_copilot/cli.py && uv run ruff format --check src/voice_copilot/cli.py && uv run mypy src/voice_copilot/cli.py`
Expected: all PASS. There is no new automated test for this task —
`_run_vc`'s orchestration is verified manually in Task 8, consistent with
the rest of `cli.py`'s existing (untested) orchestration functions.

- [ ] **Step 5: Smoke-test the other commands still work**

Run: `uv run voice-copilot version` and
`uv run voice-copilot serve --no-open --no-tray --no-hotkeys --demo` (Ctrl+C
to stop) — confirms the `_start_tts_driver` return-type change didn't break
`_serve`/`_proxy_only`.

- [ ] **Step 6: Commit**

```bash
git add src/voice_copilot/cli.py
git commit -m "feat: wire FocusRouter into vc — gates narration and push-to-talk by focus"
```

---

### Task 7: Settings checkbox + browser-side focus reporting and hotkey wiring

**Files:**
- Modify: `src/voice_copilot/web/static/index.html`
- Modify: `src/voice_copilot/web/static/app.js`

No automated test — there's no frontend test harness in this project (the
existing settings checkboxes aren't tested either); verified manually in
Task 8.

- [ ] **Step 1: Add the Settings checkbox**

In `src/voice_copilot/web/static/index.html`, inside the
`<section class="panel" data-panel="settings">` block, right after the
existing "Dialog" subsection's closing `</label>` (the one for
`dialog.deliver_immediately`) and before the section's closing `</section>`,
add:

```html
        <h3 class="section-title">Narration</h3>
        <label class="row">
          <input type="checkbox" name="focus.narrate_only_when_focused" />
          Narrate only when focused (unchecked: stays on the last
          voice-copilot window you focused, across all running instances)
        </label>
```

This works with zero JS changes — the existing `loadConfig`/`saveConfig`
in `app.js` already walk every `form.elements[i].name` as a dotted path
into the config object (see the existing `dialog.auto_pause_on_speak`
checkbox for the established pattern).

- [ ] **Step 2: Report panel focus to the server**

In `src/voice_copilot/web/static/app.js`, inside `connect()`'s `ws.onopen`
handler, change:

```javascript
    ws.onopen  = () => {
      setConn("connected");
      retryMs = 500;
      syncPlaybackRateState();
    };
```

to:

```javascript
    ws.onopen  = () => {
      setConn("connected");
      retryMs = 500;
      syncPlaybackRateState();
      send({ type: "cmd", cmd: "panel_focus", focused: document.hasFocus() });
    };
```

Right after the existing `connect();` call (the one that starts the
WebSocket connection), add:

```javascript
  window.addEventListener("focus", () => send({ type: "cmd", cmd: "panel_focus", focused: true }));
  window.addEventListener("blur",  () => send({ type: "cmd", cmd: "panel_focus", focused: false }));
```

- [ ] **Step 3: Make the OS-level hotkey actually trigger recording**

In `src/voice_copilot/web/static/app.js`'s `ws.onmessage` handler, inside
the `if (msg.type === "event")` block, right after the existing
`user.skip.requested` handling (the block that calls
`skipCurrentPlayback()`), add:

```javascript
          if (!isMini && msg.kind === "user.speak.requested") {
            const phase = (msg.payload || {}).phase;
            if (phase === "start" && !speaking) { speaking = true; startSpeak(); }
            else if (phase === "end" && speaking) { speaking = false; endSpeak(); }
            return;
          }
```

This is the previously-missing link: the OS-global hotkey
(`hotkeys.py`/pynput) publishes `user.speak.requested` onto the bus, the
server only forwards/acts on it when this instance's `FocusRouter` says
it's focused (Task 4 gates the *publish* itself, so nothing further to
check here), and this handler is what turns that into an actual
`getUserMedia` recording — mirroring exactly what the existing in-page
`Alt+Space` keydown listener already does, so the `speaking` guard
prevents the two paths from double-triggering when the panel itself is
the focused window.

- [ ] **Step 4: Commit**

```bash
git add src/voice_copilot/web/static/index.html src/voice_copilot/web/static/app.js
git commit -m "feat: add narration focus checkbox, panel focus reporting, hotkey-to-mic wiring"
```

---

### Task 8: Manual end-to-end verification

No automated test can exercise real OS window focus, a real browser tab,
and a real second `vc` process at once. Per verification-before-completion,
do not consider this plan done until every step below passes for real.

- [ ] **Step 1: Single instance, checked (default)**

Run `uv run voice-copilot vc claude` (or any catalog CLI you have
installed). Confirm narration plays while the terminal or panel is
focused. Switch focus to a third, unrelated window (e.g. a text editor) —
confirm narration stops (no new utterances spoken) until you switch back.

- [ ] **Step 2: Single instance, unchecked**

In the panel's Settings tab, uncheck "Narrate only when focused". Focus
the terminal once, then switch to an unrelated window — confirm narration
*keeps* playing (sticky), unlike Step 1.

- [ ] **Step 3: Two simultaneous instances, checked**

Run `uv run voice-copilot vc claude` in one terminal and
`uv run voice-copilot vc codex` (or any second catalog CLI) in another.
Trigger narration in both (give each agent something to do). Confirm only
the one whose terminal or panel currently has focus is audible — switching
focus between the two should switch which one speaks, with at most a
~0.5s lag (the focus poll interval).

- [ ] **Step 4: Two simultaneous instances, unchecked**

Uncheck "Narrate only when focused" in *both* panels. Focus instance A's
terminal, let it narrate, then switch focus to an unrelated window (not
instance B). Confirm A keeps narrating (sticky) and B stays silent. Then
focus instance B's terminal — confirm narration switches to B and A goes
silent, proving the cross-process sticky file (`focus-state.json` in the
config directory) correctly arbitrates between the two.

- [ ] **Step 5: Push-to-talk routes to the focused instance**

With two instances running, focus instance A and press the push-to-talk
hotkey (`alt+space` by default) — confirm only A's panel shows the mic
recording UI/sends transcribed text, not B's.

- [ ] **Step 6: Note results**

Record pass/fail for each step above before marking this plan complete.
