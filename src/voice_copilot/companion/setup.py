"""`voice-copilot setup`: connect the CLIs on this machine in one go.

The plugins ship inside the package, so nothing here needs a copy of the repository.
Each CLI has its own way to install one, and that way is the only thing this knows about
it. A CLI whose plugin can't carry the whole command set isn't offered one: `vc <cli>`
gives it the same commands through the proxy.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import voice_copilot

_TIMEOUT_S = 120
PLUGIN_ID = "voice-copilot@voice-copilot"


def bundle_root() -> Path:
    """Where the plugins and marketplaces are: inside the wheel, or in the checkout."""
    package = Path(voice_copilot.__file__).parent
    bundled = package / "_bundle"
    return bundled if bundled.is_dir() else package.parents[1]


def _run(*argv: str) -> tuple[bool, str]:
    binary = shutil.which(argv[0])
    if binary is None:
        return False, f"{argv[0]} not found"
    try:
        done = subprocess.run(
            [binary, *argv[1:]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    output = (done.stdout + done.stderr).strip()
    return done.returncode == 0, output.splitlines()[-1] if output else ""


def _steps(*steps: tuple[str, ...]) -> tuple[bool, str]:
    last = ""
    for step in steps:
        ok, last = _run(*step)
        if not ok:
            return False, last
    return True, last


def install_claude(root: Path) -> tuple[bool, str]:
    return _steps(
        ("claude", "plugin", "marketplace", "add", str(root)),
        ("claude", "plugin", "install", PLUGIN_ID),
    )


def install_codex(root: Path) -> tuple[bool, str]:
    return _steps(
        ("codex", "plugin", "marketplace", "add", str(root)),
        ("codex", "plugin", "add", PLUGIN_ID),
    )


def install_pi(root: Path) -> tuple[bool, str]:
    return _run("pi", "install", str(root / "plugins" / "pi"))


def install_omp(root: Path) -> tuple[bool, str]:
    return _run("omp", "plugin", "install", str(root / "plugins" / "pi"))


def _dsh_profiles() -> list[str]:
    home = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    profiles = home / "profiles"
    return sorted(p.name for p in profiles.iterdir() if p.is_dir()) if profiles.is_dir() else []


def install_dsh(root: Path) -> tuple[bool, str]:
    profiles = _dsh_profiles()
    if not profiles:
        return False, "no profile yet: run `dsh <profile>` once, then setup again"
    plugin = str(root / "plugins" / "deepseek-harness")
    return _steps(*(("dsh", "plugin", "--profile", name, "add", "-w", plugin) for name in profiles))


@dataclass(frozen=True)
class Target:
    binary: str
    label: str
    install: Callable[[Path], tuple[bool, str]]


TARGETS = (
    Target("claude", "Claude Code", install_claude),
    Target("codex", "Codex", install_codex),
    Target("pi", "Pi", install_pi),
    Target("omp", "oh-my-pi", install_omp),
    Target("dsh", "DeepSeek Harness", install_dsh),
)

#: Everything else narrates through the proxy, with the same `vc` words: `vc opencode`.
PROXY_ONLY = ("opencode", "gemini", "copilot", "droid", "cline", "qwen", "aider")


def trust_codex_hooks(*, cwd: str) -> tuple[bool, str]:
    """Do what `/hooks` in Codex would, at the user's say-so. Codex's own server does the writing."""
    binary = shutil.which("codex")
    if binary is None:
        return False, "codex not found"
    server = subprocess.Popen(
        [binary, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    assert server.stdin is not None and server.stdout is not None
    seq = 0

    def call(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal seq
        seq += 1
        server.stdin.write(json.dumps({"id": seq, "method": method, "params": params}) + "\n")  # type: ignore[union-attr]
        server.stdin.flush()  # type: ignore[union-attr]
        while True:
            reply = json.loads(server.stdout.readline())  # type: ignore[union-attr]
            if reply.get("id") == seq:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                result: dict[str, object] = reply["result"]
                return result

    try:
        call("initialize", {"clientInfo": {"name": "voice-copilot-setup", "version": "0"}})
        server.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        server.stdin.flush()
        listed = call("hooks/list", {"cwds": [cwd]})["data"]
        assert isinstance(listed, list)
        edits = [
            {
                "keyPath": f"hooks.state.{json.dumps(hook['key'])}.trusted_hash",
                "value": hook["currentHash"],
                "mergeStrategy": "upsert",
            }
            for hook in listed[0]["hooks"]
            if hook.get("pluginId") == PLUGIN_ID
        ]
        if not edits:
            return False, "Codex doesn't list the plugin's hooks yet"
        call("config/batchWrite", {"edits": edits, "reloadUserConfig": True})
        return True, f"{len(edits)} hooks trusted"
    except (OSError, ValueError, RuntimeError, AssertionError, KeyError, IndexError) as e:
        return False, str(e)
    finally:
        server.stdin.close()
        server.terminate()
