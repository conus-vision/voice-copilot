# `vc` PATH Alias Install Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `vc claude` work as a shorter alternative to `voice-copilot claude`, without ever silently shadowing or conflicting with an unrelated `vc` the user might already have on PATH.

**Architecture:** A one-time, idempotent check (`shutil.which("vc")`) run from `main()` before each command dispatch. If nothing is found, write a thin forwarding shim (`vc.exe`/`vc.cmd` on Windows, a symlink on POSIX) next to the already-installed `voice-copilot` script, in the same directory `uv`/`pip` already put on PATH. The result of the check is cached in a marker file so it only actually probes PATH once, not on every invocation.

**Tech Stack:** Python 3.11+ standard library only (`shutil`, `sys`, `os`, `pathlib`) — no new dependencies.

## Global Constraints

- Python 3.11+, formatted with `ruff format`, linted with `ruff`, `mypy` strict on new code.
- Package manager is `uv`.
- No hidden retries, no silent fallbacks — but this is the one place silence
  is *correct*: if `vc` already exists and isn't ours, there's nothing
  wrong to report, the existing tool is simply left in charge.
- Prefer editing existing files over creating new ones.

This plan builds on `docs/superpowers/plans/2026-06-25-vc-launch-core.md`
(the `vc` Typer command and `main()` entry point must already exist).

---

### Task 1: Detect and install the `vc` shim

**Files:**
- Create: `src/voice_copilot/alias_install.py`
- Test: `tests/unit/test_alias_install.py`

**Interfaces:**
- Produces:
  ```python
  def marker_path() -> Path: ...
  def ensure_vc_alias(*, voice_copilot_script: Path | None = None) -> None: ...
  ```
  `ensure_vc_alias()` is the only entry point Task 2's `main()` calls. It is
  a no-op (returns immediately) if `marker_path()` already exists. It never
  raises — any failure (permissions, unsupported platform) is caught and
  logged, never surfaced to the user as an error, since this is a
  convenience feature, not a requirement for `voice-copilot <name>` to
  work.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_alias_install.py
import sys

import pytest

from voice_copilot.alias_install import ensure_vc_alias, marker_path


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "voice_copilot.alias_install.config_path", lambda: tmp_path / "config.yaml"
    )
    return tmp_path


def test_marker_path_is_under_the_config_dir(isolated_config_dir) -> None:
    assert marker_path().parent == isolated_config_dir


def test_installs_shim_when_vc_is_not_on_path(isolated_config_dir, tmp_path, monkeypatch) -> None:
    script_dir = tmp_path / "Scripts"
    script_dir.mkdir()
    fake_script = script_dir / ("voice-copilot.exe" if sys.platform == "win32" else "voice-copilot")
    fake_script.write_text("", encoding="utf-8")

    monkeypatch.setattr("voice_copilot.alias_install.shutil.which", lambda name: None)

    ensure_vc_alias(voice_copilot_script=fake_script)

    expected_name = "vc.cmd" if sys.platform == "win32" else "vc"
    shim = script_dir / expected_name
    assert shim.exists()
    assert marker_path().exists()


def test_does_nothing_when_vc_already_exists(isolated_config_dir, tmp_path, monkeypatch) -> None:
    script_dir = tmp_path / "Scripts"
    script_dir.mkdir()
    fake_script = script_dir / ("voice-copilot.exe" if sys.platform == "win32" else "voice-copilot")
    fake_script.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "voice_copilot.alias_install.shutil.which",
        lambda name: r"C:\some\other\vc.exe" if sys.platform == "win32" else "/usr/local/bin/vc",
    )

    ensure_vc_alias(voice_copilot_script=fake_script)

    expected_name = "vc.cmd" if sys.platform == "win32" else "vc"
    assert not (script_dir / expected_name).exists()
    assert marker_path().exists()


def test_is_a_noop_on_second_call(isolated_config_dir, tmp_path, monkeypatch) -> None:
    script_dir = tmp_path / "Scripts"
    script_dir.mkdir()
    fake_script = script_dir / ("voice-copilot.exe" if sys.platform == "win32" else "voice-copilot")
    fake_script.write_text("", encoding="utf-8")

    which_calls = []
    monkeypatch.setattr(
        "voice_copilot.alias_install.shutil.which",
        lambda name: which_calls.append(name) or None,
    )

    ensure_vc_alias(voice_copilot_script=fake_script)
    ensure_vc_alias(voice_copilot_script=fake_script)

    assert which_calls == ["vc"]


def test_never_raises_on_unwritable_directory(isolated_config_dir, tmp_path, monkeypatch) -> None:
    script_dir = tmp_path / "Scripts"
    script_dir.mkdir()
    fake_script = script_dir / ("voice-copilot.exe" if sys.platform == "win32" else "voice-copilot")
    fake_script.write_text("", encoding="utf-8")

    monkeypatch.setattr("voice_copilot.alias_install.shutil.which", lambda name: None)

    def boom(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("voice_copilot.alias_install.Path.write_text", boom)

    ensure_vc_alias(voice_copilot_script=fake_script)  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_alias_install.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'voice_copilot.alias_install'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/voice_copilot/alias_install.py
"""One-time, idempotent install of a `vc` PATH alias for `voice-copilot`.

Never registered as a packaging entry point: pip/uv would create it
unconditionally on every install, which could silently shadow or conflict
with an unrelated `vc` the user already has. Instead this checks PATH once
at runtime and only acts if nothing is there.
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

from voice_copilot.core.config import config_path

log = logging.getLogger(__name__)

_CMD_SHIM = '@echo off\r\n"{target}" %*\r\n'
_SH_SHIM = '#!/usr/bin/env sh\nexec "{target}" "$@"\n'


def marker_path() -> Path:
    return config_path().parent / "vc-alias-checked"


def ensure_vc_alias(*, voice_copilot_script: Path | None = None) -> None:
    """Install a `vc` → `voice-copilot` shim once, if nothing else owns `vc`."""
    marker = marker_path()
    if marker.exists():
        return
    try:
        _ensure_vc_alias_unchecked(voice_copilot_script)
    except Exception as e:
        log.debug("vc alias install skipped: %s", e)
    finally:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("", encoding="utf-8")
        except OSError as e:
            log.debug("could not write vc alias marker: %s", e)


def _ensure_vc_alias_unchecked(voice_copilot_script: Path | None) -> None:
    if shutil.which("vc") is not None:
        return

    script = voice_copilot_script or _locate_voice_copilot_script()
    if script is None:
        return

    if sys.platform == "win32":
        shim = script.parent / "vc.cmd"
        shim.write_text(_CMD_SHIM.format(target=script), encoding="utf-8")
    else:
        shim = script.parent / "vc"
        shim.write_text(_SH_SHIM.format(target=script), encoding="utf-8")
        shim.chmod(0o755)


def _locate_voice_copilot_script() -> Path | None:
    found = shutil.which("voice-copilot")
    return Path(found) if found else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_alias_install.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full test suite, lint, and type-check**

Run: `uv run pytest tests/ -v && uv run ruff check src/voice_copilot/alias_install.py && uv run ruff format --check src/voice_copilot/alias_install.py && uv run mypy src/voice_copilot/alias_install.py`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/voice_copilot/alias_install.py tests/unit/test_alias_install.py
git commit -m "feat: add idempotent vc PATH alias installer"
```

---

### Task 2: Wire the check into `main()`

**Files:**
- Modify: `src/voice_copilot/cli.py`

**Interfaces:**
- Consumes: `ensure_vc_alias` (Task 1, `alias_install.py`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_cli_dispatch.py
# (append to the existing file from the vc-launch-core plan)
def test_main_calls_ensure_vc_alias(monkeypatch) -> None:
    import voice_copilot.cli as cli_module

    calls = []
    monkeypatch.setattr(cli_module, "ensure_vc_alias", lambda: calls.append(True))
    monkeypatch.setattr(cli_module, "app", lambda: None)
    monkeypatch.setattr("sys.argv", ["voice-copilot", "version"])

    cli_module.main()

    assert calls == [True]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_cli_dispatch.py::test_main_calls_ensure_vc_alias -v`
Expected: FAIL with `AttributeError: module 'voice_copilot.cli' has no attribute 'ensure_vc_alias'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/voice_copilot/cli.py`'s imports:

```python
from voice_copilot.alias_install import ensure_vc_alias
```

Change the existing `main()` (added in the vc-launch-core plan) to:

```python
def main() -> None:
    ensure_vc_alias()
    sys.argv[:] = _normalize_argv(sys.argv)
    app()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_cli_dispatch.py -v`
Expected: PASS (all tests in the file, including the new one)

- [ ] **Step 5: Run the full test suite, lint, and type-check**

Run: `uv run pytest tests/ -v && uv run ruff check src/voice_copilot/cli.py && uv run ruff format --check src/voice_copilot/cli.py && uv run mypy src/voice_copilot/cli.py`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/voice_copilot/cli.py tests/unit/test_cli_dispatch.py
git commit -m "feat: check/install the vc alias on every voice-copilot invocation"
```

---

### Task 3: Manual verification

No automated test can prove PATH actually picks up the new shim in a fresh
shell (that requires a real new shell process). Per
verification-before-completion, do not consider this plan done until this
passes for real.

- [ ] **Step 1: Verify install in a clean environment**

In a terminal where `vc` is not currently a recognized command
(`Get-Command vc` / `which vc` should fail first), run
`uv run voice-copilot version`. Open a **new** terminal (PATH changes from
this run won't apply to already-open shells) and run `vc version` —
expect the same output as `voice-copilot version`.

- [ ] **Step 2: Verify it leaves an existing `vc` alone**

On a machine (or temporary PATH entry) where `vc` already resolves to
something else, run `uv run voice-copilot version`, then check that
`vc` still resolves to the original tool, unchanged.

- [ ] **Step 3: Verify idempotency**

Delete the marker file at the path printed by
`uv run python -c "from voice_copilot.alias_install import marker_path; print(marker_path())"`,
then run `voice-copilot version` twice — confirm the shim is (re)installed
on the first run and the second run does not touch the filesystem again
(no observable difference, but check via logs with
`VOICE_COPILOT_LOG=DEBUG`).

- [ ] **Step 4: Note results**

Record pass/fail for each step above before marking this plan complete.
