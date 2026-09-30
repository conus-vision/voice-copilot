"""Connect coding CLIs to Voice Copilot through their plugin or hook systems.

Each `Integration` knows how to wire one CLI:

* for one session: `vc <cli>` and the panel's Launch add a flag or an
  environment variable (Claude Code gets a generated plugin through
  ``--plugin-dir``, Pi loads the extension with ``-e``);
* for good: `install()` puts a plugin, an extension or hook entries where
  the CLI looks for them (``voice-copilot integrate <cli>``, or the Install
  button in the panel), and `uninstall()` takes them out again.

Hook entries run the ``voice-copilot-hook`` forwarder, which finds the right
Voice Copilot instance through the environment a launch sets, so one
installation serves ``serve`` on the default port and every ``vc`` session.
Claude Code is the exception: it posts to Voice Copilot directly (HTTP hooks,
no process per event), so its permanent plugin targets one port.

Files are only ever written where the CLI documents them; a JSON file we edit
gets a one-time backup next to it, and only entries that run our forwarder
are added or removed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from voice_copilot import __version__
from voice_copilot.core.config import config_path

DEFAULT_PORT = 8765
ASSETS = Path(__file__).parent / "assets"
#: Marks every hook entry we add, so uninstall finds exactly ours.
MARKER = "voice-copilot-hook"
CLAUDE_PLUGIN = "voice-copilot"
CLAUDE_SESSION_PLUGIN = "voice-copilot-session"
CLAUDE_MARKETPLACE = "voice-copilot"

#: Hook timeouts. A paused agent waits at its next tool call.
GATE_TIMEOUT_S = 3600
TURN_TIMEOUT_S = 60
EVENT_TIMEOUT_S = 10


class IntegrationError(RuntimeError):
    """An install or uninstall that cannot go ahead; the message says why."""


def companion_url(port: int, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}/api/companion/v1"


def data_dir() -> Path:
    """Where generated plugins live (next to the config file)."""
    return config_path().parent / "integrations"


def hook_command(dialect: str, *, cli: str | None = None, event: str | None = None) -> str:
    """Shell command a CLI runs for one hook: the forwarder, found on PATH if possible."""
    exe = shutil.which(MARKER)
    if exe:
        base = f'"{exe}"' if " " in exe else exe
    else:
        # A checkout run through `uv run` has no console script on PATH.
        python = sys.executable
        python = f'"{python}"' if " " in python else python
        base = f"{python} -m voice_copilot.companion.hook"
        # Keeps MARKER in the command so uninstall still recognizes it.
        base += f" --tag {MARKER}"
    parts = [base, dialect]
    if event:
        parts.append(event)
    if cli and cli != dialect:
        parts += ["--cli", cli]
    return " ".join(parts)


def _home() -> Path:
    return Path.home()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError as e:
        raise IntegrationError(
            f"{path} is not valid JSON ({e}); fix it or add the hooks by hand"
        ) from e
    if not isinstance(data, dict):
        raise IntegrationError(f"{path} does not hold a JSON object")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".voice-copilot.bak")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _is_ours(entry: Any) -> bool:
    return MARKER in json.dumps(entry)


# --------------------------------------------------------------- Claude Code


def claude_hooks(url: str, *, launch: str | None = None, mode: str | None = None) -> dict[str, Any]:
    """hooks.json for the Claude Code plugin: every event posts to Voice Copilot.

    SessionStart is left out: Claude Code only runs command hooks for it, and
    the first event of a session starts it on our side anyway.
    """
    query = "cli=claude"
    if launch:
        query += f"&launch={launch}"
    if mode:
        query += f"&mode={mode}"

    def entry(timeout: int, matcher: str | None = None) -> dict[str, Any]:
        hook: dict[str, Any] = {
            "type": "http",
            "url": f"{url}/hooks/claude?{query}",
            "timeout": timeout,
            # A launched terminal carries its launch id; the permanent plugin
            # sends it so Voice Copilot can skip the duplicate report.
            "headers": {"X-Voice-Copilot-Launch": "$VOICE_COPILOT_LAUNCH"},
            "allowedEnvVars": ["VOICE_COPILOT_LAUNCH"],
        }
        group: dict[str, Any] = {"hooks": [hook]}
        if matcher is not None:
            group["matcher"] = matcher
        return group

    return {
        "hooks": {
            "UserPromptSubmit": [entry(EVENT_TIMEOUT_S)],
            "PreToolUse": [entry(GATE_TIMEOUT_S, "*")],
            "PostToolUse": [entry(EVENT_TIMEOUT_S, "*")],
            "PostToolUseFailure": [entry(EVENT_TIMEOUT_S, "*")],
            "MessageDisplay": [entry(5)],
            "PermissionRequest": [entry(EVENT_TIMEOUT_S, "*")],
            "Notification": [entry(EVENT_TIMEOUT_S)],
            "Stop": [entry(TURN_TIMEOUT_S)],
            "SubagentStop": [entry(EVENT_TIMEOUT_S)],
            "StopFailure": [entry(EVENT_TIMEOUT_S)],
            "SessionEnd": [entry(5)],
        }
    }


def write_claude_plugin(
    dest: Path,
    url: str,
    *,
    name: str = CLAUDE_PLUGIN,
    launch: str | None = None,
    mode: str | None = None,
) -> Path:
    """Write a Claude Code plugin directory that reports to `url`."""
    (dest / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (dest / "hooks").mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": __version__,
        "description": "Narrates this Claude Code session by voice through Voice Copilot",
        "author": {"name": "Conus Vision", "url": "https://voice-copilot.conus.vision"},
        "homepage": "https://github.com/conus-vision/voice-copilot",
        "license": "MIT",
    }
    (dest / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (dest / "hooks" / "hooks.json").write_text(
        json.dumps(claude_hooks(url, launch=launch, mode=mode), indent=2) + "\n",
        encoding="utf-8",
    )
    return dest


#: Session plugins older than this are cleaned up when a new one is written.
_SESSION_PLUGIN_TTL_S = 7 * 24 * 3600


def claude_session_plugin(port: int, launch: str, mode: str) -> Path:
    """The plugin `vc` and Launch pass with --plugin-dir: one per launch.

    Each carries its launch id, so two terminals launched from the same panel
    never share (or overwrite) one plugin.
    """
    root = data_dir() / "claude-session"
    _prune(root)
    return write_claude_plugin(
        root / launch, companion_url(port), name=CLAUDE_SESSION_PLUGIN, launch=launch, mode=mode
    )


def _prune(root: Path) -> None:
    if not root.is_dir():
        return
    cutoff = time.time() - _SESSION_PLUGIN_TTL_S
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def _claude_config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else _home() / ".claude"


# --------------------------------------------------------------- the catalog


@dataclass
class Integration:
    """How Voice Copilot hooks into one CLI."""

    id: str
    label: str
    method: str
    #: What narration gets from it.
    gives: str
    #: What Voice Copilot can do back: pause, stop, voice.
    controls: tuple[str, ...]
    #: Checked end to end against the real CLI.
    verified: bool
    docs_url: str
    #: `vc` / Launch load it for one session with nothing installed.
    session_auto: bool = False
    #: What is left for the user after the files are in place.
    after_install: tuple[str, ...] = ()
    notes: str = ""

    def target(self) -> Path | None:
        return None

    def installed(self) -> bool:
        path = self.target()
        return bool(path and path.exists())

    def install(self, *, port: int = DEFAULT_PORT) -> str:
        raise IntegrationError(f"{self.label} has no automatic install; see the steps")

    def uninstall(self) -> str:
        raise IntegrationError(f"{self.label} has no automatic uninstall; see the steps")

    def steps(self, *, port: int = DEFAULT_PORT) -> list[str]:
        """How to do by hand what `install()` does, with real paths."""
        return [step.format(port=port) for step in self.after_install]

    def prepare(self, *, port: int = DEFAULT_PORT) -> None:
        """Write whatever files `steps()` refers to, without touching the CLI's config."""

    def describe(self, *, port: int = DEFAULT_PORT) -> dict[str, Any]:
        target = self.target()
        return {
            "id": self.id,
            "label": self.label,
            "method": self.method,
            "gives": self.gives,
            "controls": list(self.controls),
            "verified": self.verified,
            "docs_url": self.docs_url,
            "session_auto": self.session_auto,
            "installed": self.installed(),
            "can_install": type(self).install is not Integration.install,
            "target": str(target) if target else None,
            "command": f"voice-copilot integrate {self.id}",
            "steps": self.steps(port=port),
            "notes": self.notes,
        }


@dataclass
class ClaudeIntegration(Integration):
    def target(self) -> Path | None:
        return data_dir() / "claude-marketplace"

    def installed(self) -> bool:
        record = _claude_config_dir() / "plugins" / "installed_plugins.json"
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        plugins = data.get("plugins") if isinstance(data, dict) else None
        return isinstance(plugins, dict) and f"{CLAUDE_PLUGIN}@{CLAUDE_MARKETPLACE}" in plugins

    def _marketplace(self, port: int) -> Path:
        root = self.target()
        assert root is not None
        write_claude_plugin(root / "plugins" / CLAUDE_PLUGIN, companion_url(port))
        (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (root / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": CLAUDE_MARKETPLACE,
                    "owner": {"name": "Conus Vision"},
                    "description": "Voice Copilot plugins",
                    "plugins": [
                        {
                            "name": CLAUDE_PLUGIN,
                            "source": f"./plugins/{CLAUDE_PLUGIN}",
                            "description": "Narrates Claude Code sessions by voice",
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return root

    def steps(self, *, port: int = DEFAULT_PORT) -> list[str]:
        root = self.target()
        return [
            f"Run `voice-copilot integrate claude --print` once: it writes the plugin to {root}.",
            f"In Claude Code, run: /plugin marketplace add {root}",
            f"Then run: /plugin install {CLAUDE_PLUGIN}@{CLAUDE_MARKETPLACE}",
            f"Keep `voice-copilot serve` running (port {port}) and use `claude` as usual.",
        ]

    def prepare(self, *, port: int = DEFAULT_PORT) -> None:
        self._marketplace(port)

    def install(self, *, port: int = DEFAULT_PORT) -> str:
        root = self._marketplace(port)
        claude = shutil.which("claude")
        if claude is None:
            raise IntegrationError(
                f"`claude` is not on PATH. The plugin is ready in {root}; inside Claude Code run "
                f"`/plugin marketplace add {root}` and then `/plugin install "
                f"{CLAUDE_PLUGIN}@{CLAUDE_MARKETPLACE}`."
            )
        steps = [
            [claude, "plugin", "marketplace", "add", str(root)],
            [claude, "plugin", "install", f"{CLAUDE_PLUGIN}@{CLAUDE_MARKETPLACE}"],
        ]
        if self.installed():
            # Same version string, new port: reinstall to refresh the cached copy.
            steps = [
                [claude, "plugin", "uninstall", f"{CLAUDE_PLUGIN}@{CLAUDE_MARKETPLACE}"],
                [claude, "plugin", "marketplace", "remove", CLAUDE_MARKETPLACE],
                *steps,
            ]
        for argv in steps:
            _run(argv, allow_fail="marketplace" in argv and "remove" in argv)
        return (
            f"Installed the {CLAUDE_PLUGIN} plugin for Claude Code (user scope), reporting to "
            f"port {port}. New Claude Code sessions are narrated while `voice-copilot serve` runs."
        )

    def uninstall(self) -> str:
        claude = shutil.which("claude")
        if claude is None:
            raise IntegrationError("`claude` is not on PATH; run `/plugin uninstall` inside Claude")
        _run(
            [claude, "plugin", "uninstall", f"{CLAUDE_PLUGIN}@{CLAUDE_MARKETPLACE}"],
            allow_fail=True,
        )
        _run([claude, "plugin", "marketplace", "remove", CLAUDE_MARKETPLACE], allow_fail=True)
        return "Removed the Voice Copilot plugin from Claude Code."


def _run(argv: list[str], *, allow_fail: bool = False) -> None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise IntegrationError(f"`{' '.join(argv)}` failed: {e}") from e
    if done.returncode != 0 and not allow_fail:
        detail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
        raise IntegrationError(f"`{' '.join(argv)}` failed: {detail[0]}")


@dataclass
class FileIntegration(Integration):
    """A single plugin file the CLI loads from a directory."""

    asset: str = ""
    dest: tuple[str, ...] = ()
    #: Environment variable that relocates the CLI's config directory.
    env_dir: str | None = None
    #: Path of `dest` under `env_dir` when that variable is set.
    env_dest: tuple[str, ...] = ()

    def target(self) -> Path | None:
        base = os.environ.get(self.env_dir) if self.env_dir else None
        if base:
            return Path(base).joinpath(*self.env_dest)
        return _home().joinpath(*self.dest)

    def steps(self, *, port: int = DEFAULT_PORT) -> list[str]:
        return [f"Copy {ASSETS / self.asset} to {self.target()}", *super().steps(port=port)]

    def install(self, *, port: int = DEFAULT_PORT) -> str:
        path = self.target()
        assert path is not None
        source = ASSETS / self.asset
        path.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if path.exists():
                shutil.rmtree(path)
            shutil.copytree(source, path, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, path)
        return f"Copied the Voice Copilot {self.method} to {path}."

    def uninstall(self) -> str:
        path = self.target()
        assert path is not None
        if not path.exists():
            return f"Nothing to remove at {path}."
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return f"Removed {path}."


@dataclass
class HooksJsonIntegration(Integration):
    """Claude-format hook entries merged into the CLI's JSON settings."""

    dialect: str = "claude"
    dest: tuple[str, ...] = ()
    env_dir: str | None = None
    env_dest: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    tool_events: tuple[str, ...] = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
    gate_events: tuple[str, ...] = ("PreToolUse",)
    matcher: str = "*"
    #: Gemini counts hook timeouts in milliseconds.
    timeout_scale: int = 1
    #: Extra keys every hook entry needs (Gemini wants a name).
    extra: dict[str, Any] = field(default_factory=dict)

    def target(self) -> Path | None:
        base = os.environ.get(self.env_dir) if self.env_dir else None
        if base:
            return Path(base).joinpath(*self.env_dest)
        return _home().joinpath(*self.dest)

    def installed(self) -> bool:
        path = self.target()
        if path is None or not path.exists():
            return False
        try:
            hooks = _read_json(path).get("hooks")
        except IntegrationError:
            return False
        return _is_ours(hooks or {})

    def _entry(self, event: str) -> dict[str, Any]:
        timeout = GATE_TIMEOUT_S if event in self.gate_events else TURN_TIMEOUT_S
        hook: dict[str, Any] = {
            "type": "command",
            "command": hook_command(self.dialect, cli=self.id),
            "timeout": timeout * self.timeout_scale,
            **self.extra,
        }
        group: dict[str, Any] = {"hooks": [hook]}
        if event in self.tool_events:
            group["matcher"] = self.matcher
        return group

    def snippet(self) -> dict[str, Any]:
        return {"hooks": {event: [self._entry(event)] for event in self.events}}

    def steps(self, *, port: int = DEFAULT_PORT) -> list[str]:
        return [
            f"Merge these hooks into {self.target()}:",
            json.dumps(self.snippet(), indent=2),
            *super().steps(port=port),
        ]

    def install(self, *, port: int = DEFAULT_PORT) -> str:
        path = self.target()
        assert path is not None
        data = _read_json(path)
        hooks = data.get("hooks")
        if hooks is None:
            hooks = {}
        if not isinstance(hooks, dict):
            raise IntegrationError(f'"hooks" in {path} is not an object; add the hooks by hand')
        for event in self.events:
            groups = [g for g in hooks.get(event) or [] if not _is_ours(g)]
            groups.append(self._entry(event))
            hooks[event] = groups
        data["hooks"] = hooks
        _write_json(path, data)
        return f"Added Voice Copilot hooks for {len(self.events)} events to {path}."

    def uninstall(self) -> str:
        path = self.target()
        assert path is not None
        if not path.exists():
            return f"Nothing to remove: {path} does not exist."
        data = _read_json(path)
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return f"No hooks in {path}."
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            kept = [g for g in groups if not _is_ours(g)]
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
        if not hooks:
            del data["hooks"]
        _write_json(path, data)
        return f"Removed the Voice Copilot hooks from {path}."


@dataclass
class OwnHooksFileIntegration(HooksJsonIntegration):
    """A hooks file of our own in a directory the CLI scans (Grok Build, Copilot)."""

    #: Copilot's layout: {"version": 1, "hooks": {event: [{type, bash, ...}]}}.
    copilot: bool = False

    def installed(self) -> bool:
        path = self.target()
        return bool(path and path.exists())

    def snippet(self) -> dict[str, Any]:
        if not self.copilot:
            return super().snippet()
        hooks: dict[str, Any] = {}
        for event in self.events:
            command = hook_command("copilot", event=event)
            hooks[event] = [
                {
                    "type": "command",
                    "bash": command,
                    "powershell": command,
                    "timeoutSec": GATE_TIMEOUT_S if event in self.gate_events else TURN_TIMEOUT_S,
                }
            ]
        return {"version": 1, "hooks": hooks}

    def steps(self, *, port: int = DEFAULT_PORT) -> list[str]:
        return [f"Save this as {self.target()}:", json.dumps(self.snippet(), indent=2)]

    def install(self, *, port: int = DEFAULT_PORT) -> str:
        path = self.target()
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.snippet(), indent=2) + "\n", encoding="utf-8")
        return f"Wrote {path}."

    def uninstall(self) -> str:
        path = self.target()
        assert path is not None
        if path.exists():
            path.unlink()
            return f"Removed {path}."
        return f"Nothing to remove at {path}."


@dataclass
class KimiIntegration(Integration):
    """Kimi CLI keeps hooks as [[hooks]] tables in config.toml."""

    events: tuple[str, ...] = ()

    def target(self) -> Path | None:
        return _home() / ".kimi" / "config.toml"

    _BEGIN = "# >>> voice-copilot hooks >>>"
    _END = "# <<< voice-copilot hooks <<<"

    def installed(self) -> bool:
        path = self.target()
        return bool(path and path.exists() and self._BEGIN in path.read_text(encoding="utf-8"))

    def _strip(self, text: str) -> str:
        start = text.find(self._BEGIN)
        end = text.find(self._END)
        if start == -1 or end == -1:
            return text
        return (text[:start].rstrip() + "\n" + text[end + len(self._END) :].lstrip("\n")).lstrip(
            "\n"
        )

    def _block(self) -> str:
        command = hook_command("claude", cli="kimi").replace("\\", "\\\\").replace('"', '\\"')
        lines = [self._BEGIN]
        for event in self.events:
            timeout = GATE_TIMEOUT_S if event == "PreToolUse" else 30
            lines += [
                "[[hooks]]",
                f'event = "{event}"',
                f'command = "{command}"',
                f"timeout = {timeout}",
                "",
            ]
        lines.append(self._END)
        return "\n".join(lines)

    def steps(self, *, port: int = DEFAULT_PORT) -> list[str]:
        return [f"Append this to {self.target()}:", self._block()]

    def install(self, *, port: int = DEFAULT_PORT) -> str:
        path = self.target()
        assert path is not None
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        text = self._strip(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_name(path.name + ".voice-copilot.bak")
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
        prefix = text.rstrip() + "\n\n" if text.strip() else ""
        path.write_text(prefix + self._block() + "\n", encoding="utf-8")
        return f"Added Voice Copilot hooks to {path}."

    def uninstall(self) -> str:
        path = self.target()
        assert path is not None
        if not path.exists():
            return f"Nothing to remove: {path} does not exist."
        path.write_text(self._strip(path.read_text(encoding="utf-8")), encoding="utf-8")
        return f"Removed the Voice Copilot hooks from {path}."


_CLAUDE_STYLE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)

_INTEGRATIONS: list[Integration] = [
    ClaudeIntegration(
        id="claude",
        label="Claude Code",
        method="plugin with HTTP hooks",
        gives="your prompts, the visible answer, tool calls with results, permission prompts",
        controls=("pause", "stop", "voice"),
        verified=True,
        docs_url="https://code.claude.com/docs/en/hooks",
        session_auto=True,
        notes="Thinking is not passed to hooks; `vc claude` still narrates it through the proxy.",
    ),
    FileIntegration(
        id="pi",
        label="Pi",
        method="extension",
        gives="your prompts, streamed text and thinking, tool calls with results",
        controls=("pause", "stop", "voice"),
        verified=True,
        docs_url="https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md",
        session_auto=True,
        asset="pi/voice-copilot.ts",
        dest=(".pi", "agent", "extensions", "voice-copilot.ts"),
        env_dir="PI_CODING_AGENT_DIR",
        env_dest=("extensions", "voice-copilot.ts"),
        notes="`vc pi` loads the extension for one session: nothing to install for that.",
    ),
    FileIntegration(
        id="opencode",
        label="OpenCode",
        method="plugin",
        gives="your prompts, streamed text and reasoning, tool calls, permission prompts",
        controls=("pause", "stop", "voice"),
        verified=True,
        docs_url="https://opencode.ai/docs/plugins/",
        asset="opencode/voice-copilot.ts",
        dest=(".config", "opencode", "plugins", "voice-copilot.ts"),
        env_dir="XDG_CONFIG_HOME",
        env_dest=("opencode", "plugins", "voice-copilot.ts"),
        after_install=("Restart OpenCode so it loads the plugin.",),
        notes="For one project only, put the file in .opencode/plugins/ instead.",
    ),
    FileIntegration(
        id="hermes",
        label="Hermes Agent",
        method="Python plugin",
        gives="your prompts, tool calls with results, the answer, approval prompts",
        controls=("pause", "stop", "voice"),
        verified=True,
        docs_url="https://hermes-agent.nousresearch.com/docs/",
        asset="hermes/voice_copilot",
        dest=(".hermes", "plugins", "voice_copilot"),
        env_dir="HERMES_HOME",
        env_dest=("plugins", "voice_copilot"),
        after_install=(
            "Enable it in ~/.hermes/config.yaml: plugins: {{ enabled: [voice_copilot] }}",
        ),
        notes="Hermes 0.19 passes no streamed text or reasoning to plugins, so the answer is "
        "spoken when it is done.",
    ),
    HooksJsonIntegration(
        id="codex",
        label="Codex CLI",
        method="hooks in ~/.codex/hooks.json",
        gives="your prompts, tool calls with results, the final answer",
        controls=("stop", "voice"),
        verified=False,
        docs_url="https://github.com/openai/codex/tree/main/codex-rs/hooks",
        dest=(".codex", "hooks.json"),
        env_dir="CODEX_HOME",
        env_dest=("hooks.json",),
        events=(*_CLAUDE_STYLE_EVENTS, "PermissionRequest"),
        tool_events=("PreToolUse", "PostToolUse", "PermissionRequest"),
        after_install=(
            "Open Codex and approve the new hooks in /hooks: Codex runs a hook only after you "
            "trust it.",
        ),
        notes="Codex streams no text to hooks; `vc codex` narrates the answer through the proxy.",
    ),
    HooksJsonIntegration(
        id="gemini",
        label="Gemini CLI",
        method="hooks in ~/.gemini/settings.json",
        gives="your prompts, tool calls with results, the final answer",
        controls=("pause", "stop", "voice"),
        verified=False,
        docs_url="https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md",
        dialect="gemini",
        dest=(".gemini", "settings.json"),
        events=(
            "SessionStart",
            "BeforeAgent",
            "BeforeTool",
            "AfterTool",
            "AfterAgent",
            "Notification",
            "SessionEnd",
        ),
        tool_events=("BeforeTool", "AfterTool"),
        gate_events=("BeforeTool",),
        matcher=".*",
        timeout_scale=1000,
        extra={"name": "voice-copilot"},
    ),
    HooksJsonIntegration(
        id="qwen",
        label="Qwen Code",
        method="hooks in ~/.qwen/settings.json",
        gives="your prompts, the visible answer as it streams, tool calls, permission prompts",
        controls=("pause", "stop", "voice"),
        verified=False,
        docs_url="https://github.com/QwenLM/qwen-code/blob/main/docs/users/features/hooks.md",
        dest=(".qwen", "settings.json"),
        events=(
            *_CLAUDE_STYLE_EVENTS,
            "PostToolUseFailure",
            "MessageDisplay",
            "Notification",
            "PermissionRequest",
            "StopFailure",
        ),
        tool_events=("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest"),
    ),
    OwnHooksFileIntegration(
        id="copilot",
        label="Copilot CLI",
        method="hooks file in ~/.copilot/hooks/",
        gives="your prompts, tool calls with results, permission prompts",
        controls=("pause", "stop", "voice"),
        verified=False,
        docs_url="https://docs.github.com/en/copilot/reference/hooks-reference",
        dest=(".copilot", "hooks", "voice-copilot.json"),
        copilot=True,
        events=(
            "sessionStart",
            "userPromptSubmitted",
            "preToolUse",
            "postToolUse",
            "agentStop",
            "errorOccurred",
            "notification",
            "permissionRequest",
            "sessionEnd",
        ),
        gate_events=("preToolUse",),
    ),
    OwnHooksFileIntegration(
        id="grok",
        label="Grok Build",
        method="hooks file in ~/.grok/hooks/",
        gives="your prompts, tool calls with results, the final answer",
        controls=("pause", "stop", "voice"),
        verified=False,
        docs_url="https://github.com/xai-org/grok-build",
        dest=(".grok", "hooks", "voice-copilot.json"),
        events=(*_CLAUDE_STYLE_EVENTS, "PostToolUseFailure", "Notification", "StopFailure"),
    ),
    HooksJsonIntegration(
        id="droid",
        label="Droid",
        method="hooks in ~/.factory/settings.json",
        gives="your prompts, tool calls with results, the final answer",
        controls=("pause", "stop", "voice"),
        verified=False,
        docs_url="https://docs.factory.ai/reference/hooks-reference",
        dest=(".factory", "settings.json"),
        events=(*_CLAUDE_STYLE_EVENTS, "Notification"),
        tool_events=("PreToolUse", "PostToolUse"),
    ),
    HooksJsonIntegration(
        id="openhands",
        label="OpenHands CLI",
        method="hooks in ~/.openhands/hooks.json",
        gives="your prompts, tool calls with results, the final answer",
        controls=("pause", "stop"),
        verified=False,
        docs_url="https://github.com/OpenHands/software-agent-sdk",
        dest=(".openhands", "hooks.json"),
        events=_CLAUDE_STYLE_EVENTS,
        tool_events=("PreToolUse", "PostToolUse"),
    ),
    KimiIntegration(
        id="kimi",
        label="Kimi CLI",
        method="hooks in ~/.kimi/config.toml",
        gives="your prompts, tool calls with results, the final answer",
        controls=("pause", "stop"),
        verified=False,
        docs_url="https://github.com/MoonshotAI/kimi-cli/blob/main/docs/en/customization/hooks.md",
        events=(*_CLAUDE_STYLE_EVENTS, "PostToolUseFailure", "Notification"),
        notes="Kimi hooks are in beta.",
    ),
]

_BY_ID = {integration.id: integration for integration in _INTEGRATIONS}


def all_integrations() -> list[Integration]:
    return list(_INTEGRATIONS)


def get(cli: str) -> Integration:
    return _BY_ID[cli]


def describe_all(*, port: int = DEFAULT_PORT) -> list[dict[str, Any]]:
    return [integration.describe(port=port) for integration in _INTEGRATIONS]


# ------------------------------------------------------- one-session launches

#: CLIs whose session plugin reports everything the proxy would (text,
#: thinking, tools): `vc` narrates them through the plugin and skips the proxy.
_PLUGIN_NARRATES = frozenset({"pi"})


def plugin_narrates(cli: str) -> bool:
    return cli in _PLUGIN_NARRATES


@dataclass
class SessionWiring:
    """What a launch adds so the child CLI reports to this instance."""

    args: list[str]
    env: dict[str, str]
    note: str = ""


def session_wiring(cli: str, *, port: int, launch_id: str, proxied: bool) -> SessionWiring:
    """Arguments and environment for launching `cli` narrated by this instance.

    Every launch gets the environment, so hooks installed for good find this
    instance; Claude Code and Pi also get their plugin for this one session.
    """
    if cli not in _BY_ID:
        # A shell or an unknown program: whatever runs inside it is not ours
        # to label, and a launch id would hide a permanently installed
        # Claude Code plugin in that shell.
        return SessionWiring([], {})
    mode = "control" if proxied else "narrate"
    env = {
        "VOICE_COPILOT_URL": companion_url(port),
        "VOICE_COPILOT_LAUNCH": launch_id,
        "VOICE_COPILOT_MODE": mode,
    }
    if cli == "claude":
        plugin = claude_session_plugin(port, launch_id, mode)
        return SessionWiring(
            ["--plugin-dir", str(plugin)],
            env,
            "Voice Copilot plugin loaded: permission prompts are narrated too.",
        )
    if cli == "pi":
        return SessionWiring(
            ["-e", str(ASSETS / "pi" / "voice-copilot.ts")],
            env,
            "Narrated through the Voice Copilot extension for Pi.",
        )
    return SessionWiring([], env)
