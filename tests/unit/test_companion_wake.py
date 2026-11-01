"""Waking an idle CLI: the user's words must reach it untouched by any shell."""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_copilot.companion import wake

_NPM_SHIM = (
    "@ECHO off\r\nSETLOCAL\r\nSET dp0=%~dp0\r\n"
    'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
    '"%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n'
)


def test_a_native_binary_runs_directly() -> None:
    assert wake._direct_command("C:/tools/codex.exe") == ["C:/tools/codex.exe"]
    assert wake._direct_command("/usr/local/bin/codex") == ["/usr/local/bin/codex"]


def test_an_npm_shim_is_bypassed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim = tmp_path / "codex.cmd"
    shim.write_text(_NPM_SHIM, encoding="utf-8")
    monkeypatch.setattr(wake.shutil, "which", lambda name: "C:/node/node.exe")
    node, script = wake._direct_command(str(shim))
    assert node == "C:/node/node.exe"
    # The shim's own separators, which only Windows reads as separators.
    assert Path(script.replace("\\", "/")) == tmp_path / "node_modules/@openai/codex/bin/codex.js"


def test_an_unknown_shim_is_refused(tmp_path: Path) -> None:
    # Anything else would hand the user's words to cmd.exe to parse.
    shim = tmp_path / "codex.cmd"
    shim.write_text("@echo off\r\nsomething-else.exe %*\r\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="safely"):
        wake._direct_command(str(shim))


def test_no_node_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim = tmp_path / "codex.cmd"
    shim.write_text(_NPM_SHIM, encoding="utf-8")
    monkeypatch.setattr(wake.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="safely"):
        wake._direct_command(str(shim))


async def test_missing_codex_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wake.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="not on PATH"):
        await wake.wake_codex("thread", "hello")
