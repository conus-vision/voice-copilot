"""Launching a CLI in a new terminal window, and removing shims."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from voice_copilot.core.config import load_config
from voice_copilot.proxy import cli_shims


def test_macos_launch_keeps_non_ascii_folder_names(monkeypatch: pytest.MonkeyPatch) -> None:
    # AppleScript string literals understand \" and \\ but not \uXXXX: with
    # json.dumps' default ASCII escaping, `cd` got a mangled path and the
    # agent started in the new terminal's home directory instead.
    launched: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli_shims.subprocess, "Popen", lambda argv, **_: launched.append(argv))

    cli_shims._launch_posix_terminal(
        binary_path="/usr/local/bin/claude",
        env_overrides={"ANTHROPIC_BASE_URL": "http://127.0.0.1:8766/anthropic"},
        working_directory=Path("/Users/me/Проекты/app"),
        title="voice-copilot - Claude Code",
    )

    script = launched[0][2]
    assert "Проекты" in script
    assert "\\u" not in script


def test_launch_command_refuses_to_run_outside_the_working_directory() -> None:
    work = Path("/work/app")  # reads \work\app on Windows, where the test runs too
    command = cli_shims._render_shell_launch_command(
        binary_path="/usr/local/bin/claude",
        env_overrides={},
        working_directory=work,
    )
    assert command.startswith(f"cd {shlex.quote(str(work))} || exit 1;")


def test_restore_without_any_shim_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing was ever installed: removing a shim must not crash on the
    # missing directory, and it still takes the PATH entry out.
    removed: list[Any] = []
    monkeypatch.setattr(cli_shims, "proxy_shim_dir", lambda: tmp_path / "proxy-shims")
    monkeypatch.setattr(cli_shims, "_remove_user_path_entry", removed.append)
    monkeypatch.setattr(cli_shims, "describe_cli_shims", lambda cfg, **_: {"ok": True})

    result = cli_shims.restore_cli_shim("claude", load_config(tmp_path / "missing.yaml"))
    assert result == {"ok": True}
    assert removed == [tmp_path / "proxy-shims"]
