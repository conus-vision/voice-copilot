"""Install/remove PATH shims for popular CLI tools.

The shim approach keeps the user's original CLI configuration untouched:

* install: create a lightweight `claude.cmd` / `codex.cmd` / shell wrapper in a
    managed directory and prepend that directory to the user's PATH.
* restore: remove the wrapper; if no wrappers remain, remove the managed
    directory from PATH.

Each wrapper injects only process-local proxy settings. Most CLIs use a
`*_BASE_URL` env var; OpenCode uses a runtime `OPENCODE_CONFIG_CONTENT`
override so Zen models can be redirected without editing `opencode.json`.
"""

from __future__ import annotations

import ctypes
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from voice_copilot.core.config import (
    Config,
    ProxyCliProfileConfig,
    config_path,
    default_proxy_cli_profiles,
)
from voice_copilot.proxy.cli_catalog import CLI_CATALOG, CliCatalogEntry
from voice_copilot.proxy.server import base_urls_for

_winreg: Any | None
try:
    import winreg as _imported_winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    _winreg = None
else:
    _winreg = _imported_winreg

winreg: Any | None = _winreg

_WINDOWS_ENV_KEY = r"Environment"
_HWND_BROADCAST = 0xFFFF
_WM_SETTINGCHANGE = 0x001A
_SMTO_ABORTIFHUNG = 0x0002
_DEFAULT_PROXY_HOST = "127.0.0.1"
# Claude Code disables on-demand tool loading (deferral) when ANTHROPIC_BASE_URL
# points at a custom host and ENABLE_TOOL_SEARCH is unset, materializing every
# MCP/system tool schema into its context window (tens of K tokens). Setting it
# when we launch Claude Code through the proxy keeps deferral on. Scoped to the
# `claude` profile — the knob is Claude-Code-specific, so it must not leak into
# other anthropic-provider CLIs (e.g. aider).
_CLAUDE_PROFILE_ID = "claude"
_TOOL_SEARCH_ENV = "ENABLE_TOOL_SEARCH"
_TOOL_SEARCH_DEFAULT = "true"
_POSIX_PATH_MARKER_BEGIN = "# >>> voice-copilot proxy shims >>>"
_POSIX_PATH_MARKER_END = "# <<< voice-copilot proxy shims <<<"


def describe_cli_shims(
    cfg: Config,
    *,
    host: str = _DEFAULT_PROXY_HOST,
    port: int = 8766,
) -> dict[str, Any]:
    shim_dir = proxy_shim_dir()
    supported = _is_supported()
    path_active = _path_contains_user_entry(shim_dir) if supported else False
    resolved_working_directory = _working_directory_from_config(cfg)
    profiles = []
    for profile_id, meta in CLI_CATALOG.items():
        profile = _profile_from_config(cfg, profile_id)
        is_shell = meta.kind == "shell"
        shim_path = None if is_shell else _shim_path(meta.command)
        profiles.append(
            {
                "id": profile_id,
                "label": meta.label,
                "command": meta.command,
                "description": meta.description,
                "website_url": meta.website_url,
                "kind": meta.kind,
                "icon": meta.icon,
                "accent": meta.accent,
                "order": meta.order,
                "provider": profile.provider,
                "base_url_env": profile.base_url_env,
                "binary_path": profile.binary_path,
                "proxy_url": f"http://{host}:{port}/*"
                if is_shell
                else _proxy_url_for(profile.provider, host=host, port=port),
                "resolved_binary": _resolve_launch_binary(meta, profile, shim_dir),
                "shim_path": str(shim_path) if shim_path is not None else None,
                "installed": bool(shim_path is not None and shim_path.exists()),
                "working_directory": cfg.proxy_cli.working_directory,
                "resolved_working_directory": str(resolved_working_directory)
                if resolved_working_directory
                else None,
            }
        )
    return {
        "supported": supported,
        "platform": os.name,
        "shim_dir": str(shim_dir),
        "path_active": path_active,
        "working_directory": cfg.proxy_cli.working_directory,
        "resolved_working_directory": str(resolved_working_directory)
        if resolved_working_directory
        else None,
        "profiles": profiles,
    }


def install_cli_shim(
    profile_id: str,
    cfg: Config,
    *,
    host: str = _DEFAULT_PROXY_HOST,
    port: int = 8766,
) -> dict[str, Any]:
    _require_supported()
    meta = _meta_for(profile_id)
    _reject_shell_shim(meta)
    profile = _profile_from_config(cfg, profile_id)
    shim_dir = proxy_shim_dir()
    resolved_binary = _resolve_binary_path(meta.command, profile.binary_path, shim_dir)
    if not resolved_binary:
        raise RuntimeError(
            f"could not resolve `{meta.command}` on PATH; set a Binary override first"
        )

    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_path = _shim_path(meta.command)
    env_overrides = _proxy_env_overrides(profile_id, profile, meta=meta, host=host, port=port)
    launch_args = _proxy_launch_args(profile, meta=meta, host=host, port=port)
    if os.name == "nt":
        shim_path.write_text(
            _render_cmd_shim(
                binary_path=resolved_binary,
                env_overrides=env_overrides,
                launch_args=launch_args,
            ),
            encoding="utf-8",
        )
    else:
        shim_path.write_text(
            _render_shell_shim(
                binary_path=resolved_binary,
                env_overrides=env_overrides,
                launch_args=launch_args,
            ),
            encoding="utf-8",
        )
        shim_path.chmod(0o755)
    _add_user_path_entry(shim_dir)
    return describe_cli_shims(cfg, host=host, port=port)


def restore_cli_shim(
    profile_id: str,
    cfg: Config,
    *,
    host: str = _DEFAULT_PROXY_HOST,
    port: int = 8766,
) -> dict[str, Any]:
    _require_supported()
    meta = _meta_for(profile_id)
    _reject_shell_shim(meta)
    shim_path = _shim_path(meta.command)
    if shim_path.exists():
        shim_path.unlink()
    shim_dir = proxy_shim_dir()
    remaining_shims = shim_dir.glob("*.cmd") if os.name == "nt" else shim_dir.iterdir()
    if not any(path.is_file() for path in remaining_shims):
        _remove_user_path_entry(shim_dir)
    return describe_cli_shims(cfg, host=host, port=port)


def launch_cli_profile(
    profile_id: str,
    cfg: Config,
    *,
    host: str = _DEFAULT_PROXY_HOST,
    port: int = 8766,
) -> dict[str, Any]:
    _require_supported()
    meta = _meta_for(profile_id)
    profile = _profile_from_config(cfg, profile_id)
    resolved_binary = _resolve_launch_binary(meta, profile, proxy_shim_dir())
    if not resolved_binary:
        raise RuntimeError(_missing_binary_message(meta))
    working_directory = _working_directory_from_config(cfg, profile)
    if working_directory is None:
        raise RuntimeError("working directory does not exist; choose another folder")
    env_overrides = _proxy_env_overrides(profile_id, profile, meta=meta, host=host, port=port)
    launch_args = _proxy_launch_args(profile, meta=meta, host=host, port=port)
    title = f"voice-copilot - {meta.label}"
    if os.name == "nt":
        shell = _powershell_path()
        # A shell profile *is* the terminal: PowerShell stays open on -NoExit,
        # so there is nothing left to run inside it.
        launch_command = _render_powershell_launch(
            binary_path=None if meta.kind == "shell" else resolved_binary,
            env_overrides=env_overrides,
            working_directory=working_directory,
            title=title,
            launch_args=launch_args,
        )
        subprocess.Popen(
            [shell, "-NoExit", "-Command", launch_command],
            cwd=str(working_directory),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
    else:
        _launch_posix_terminal(
            binary_path=resolved_binary,
            env_overrides=env_overrides,
            working_directory=working_directory,
            title=title,
            launch_args=launch_args,
        )
    return {
        "ok": True,
        "profile_id": profile_id,
        "label": meta.label,
        "working_directory": str(working_directory),
        "binary_path": resolved_binary,
        "proxy_url": f"http://{host}:{port}/*"
        if meta.kind == "shell"
        else _proxy_url_for(profile.provider, host=host, port=port),
    }


@dataclass(frozen=True)
class ResolvedCli:
    profile_id: str
    label: str
    resolved_binary: str
    env_overrides: dict[str, str]
    working_directory: Path | None
    provider: str
    #: Inserted between the binary and the user's own args at launch.
    launch_args: tuple[str, ...] = ()


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
    meta: CliCatalogEntry | None
    if name in CLI_CATALOG:
        profile_id = name
        meta = CLI_CATALOG[name]
        command = meta.command
        label = meta.label
    else:
        catalog_match = next(
            ((pid, entry) for pid, entry in CLI_CATALOG.items() if entry.command == name),
            None,
        )
        if catalog_match is not None:
            profile_id, meta = catalog_match
            command = meta.command
            label = meta.label
        elif name in cfg.proxy_cli.profiles:
            profile_id, command, label = name, name, name
            meta = None
        else:
            return None

    profile = _profile_from_config(cfg, profile_id)
    resolved_binary = (
        _resolve_launch_binary(meta, profile, proxy_shim_dir())
        if meta is not None
        else _resolve_binary_path(command, profile.binary_path, proxy_shim_dir())
    )
    if not resolved_binary:
        raise RuntimeError(f"could not resolve `{command}` on PATH; set a Binary override first")

    env_overrides = _proxy_env_overrides(profile_id, profile, meta=meta, host=host, port=port)
    working_directory = _working_directory_from_config(cfg, profile)
    return ResolvedCli(
        profile_id=profile_id,
        label=label,
        resolved_binary=resolved_binary,
        env_overrides=env_overrides,
        working_directory=working_directory,
        provider=profile.provider,
        launch_args=_proxy_launch_args(profile, meta=meta, host=host, port=port),
    )


def choose_cli_working_directory(initial_dir: str | None = None) -> str | None:
    _require_supported()
    if os.name != "nt":
        return _choose_directory_tk(initial_dir)
    shell = _powershell_path()
    selected_path = _resolve_working_directory(initial_dir) or Path.home().resolve()
    command = _render_folder_picker_command(selected_path)
    result = subprocess.run(
        [shell, "-NoProfile", "-STA", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "folder picker failed"
        raise RuntimeError(detail)
    path = result.stdout.strip()
    return path or None


def proxy_shim_dir() -> Path:
    return config_path().parent / "proxy-shims"


def _meta_for(profile_id: str) -> CliCatalogEntry:
    try:
        return CLI_CATALOG[profile_id]
    except KeyError as exc:
        raise KeyError(f"unknown CLI profile {profile_id!r}") from exc


def _profile_from_config(cfg: Config, profile_id: str) -> ProxyCliProfileConfig:
    profile = cfg.proxy_cli.profiles.get(profile_id)
    if profile is None:
        try:
            return default_proxy_cli_profiles()[profile_id]
        except KeyError as exc:
            raise KeyError(f"missing CLI profile {profile_id!r}") from exc
    return profile


def _working_directory_from_config(
    cfg: Config,
    profile: ProxyCliProfileConfig | None = None,
) -> Path | None:
    override = cfg.proxy_cli.working_directory or (profile.working_directory if profile else None)
    return _resolve_working_directory(override)


def _proxy_url_for(provider: str, *, host: str, port: int) -> str:
    urls = base_urls_for(host, port)
    key = {
        "anthropic": "ANTHROPIC_BASE_URL",
        "openai": "OPENAI_BASE_URL",
        "openai-chatgpt": "OPENAI_CHATGPT_BASE_URL",
        "openrouter": "OPENROUTER_BASE_URL",
        "groq": "GROQ_BASE_URL",
        "mistral": "MISTRAL_BASE_URL",
        "deepseek": "DEEPSEEK_BASE_URL",
        "ollama": "OLLAMA_BASE_URL",
        "gemini": "GEMINI_BASE_URL",
        "opencode-zen": "OPENCODE_ZEN_BASE_URL",
    }[provider]
    return urls[key]


def _shell_env_overrides(*, host: str, port: int) -> dict[str, str]:
    """Every base-URL var at once, for the plain `terminal` profile.

    A shell has no single upstream: whatever the user starts inside it should
    be proxied, so we export one variable per route. Gemini gets both spellings
    because the CLI reads GOOGLE_GEMINI_BASE_URL while our route table calls it
    GEMINI_BASE_URL.
    """
    urls = base_urls_for(host, port)
    overrides = {
        name: urls[name]
        for name in (
            "ANTHROPIC_BASE_URL",
            "OPENAI_BASE_URL",
            "OPENROUTER_BASE_URL",
            "GROQ_BASE_URL",
            "MISTRAL_BASE_URL",
            "DEEPSEEK_BASE_URL",
            "OLLAMA_BASE_URL",
            "GEMINI_BASE_URL",
        )
    }
    overrides["GOOGLE_GEMINI_BASE_URL"] = urls["GEMINI_BASE_URL"]
    return overrides


def _proxy_launch_args(
    profile: ProxyCliProfileConfig,
    *,
    meta: CliCatalogEntry | None,
    host: str,
    port: int,
) -> tuple[str, ...]:
    """Catalog `launch_args` with `{proxy_url}` filled in for this run.

    A shell profile has no single upstream to point at, so it gets none.
    """
    if meta is None or meta.kind == "shell" or not meta.launch_args:
        return ()
    proxy_url = _proxy_url_for(profile.provider, host=host, port=port)
    return tuple(arg.replace("{proxy_url}", proxy_url) for arg in meta.launch_args)


def _proxy_env_overrides(
    profile_id: str,
    profile: ProxyCliProfileConfig,
    *,
    meta: CliCatalogEntry | None = None,
    host: str,
    port: int,
) -> dict[str, str]:
    if meta is not None and meta.kind == "shell":
        shell_overrides = _shell_env_overrides(host=host, port=port)
        if not os.environ.get(_TOOL_SEARCH_ENV, "").strip():
            shell_overrides[_TOOL_SEARCH_ENV] = _TOOL_SEARCH_DEFAULT
        return shell_overrides
    proxy_url = _proxy_url_for(profile.provider, host=host, port=port)
    if profile_id == "opencode" and profile.provider == "opencode-zen":
        return {
            "OPENCODE_CONFIG_CONTENT": json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "provider": {
                        "opencode": {
                            "options": {
                                "baseURL": proxy_url,
                            }
                        }
                    },
                },
                separators=(",", ":"),
            )
        }
    overrides = {profile.base_url_env: proxy_url}
    # Keep Claude Code's tool deferral on behind the proxy, unless the user has
    # already set their own ENABLE_TOOL_SEARCH value — then theirs wins.
    if profile_id == _CLAUDE_PROFILE_ID and not os.environ.get(_TOOL_SEARCH_ENV, "").strip():
        overrides[_TOOL_SEARCH_ENV] = _TOOL_SEARCH_DEFAULT
    return overrides


def _resolve_launch_binary(
    meta: CliCatalogEntry,
    profile: ProxyCliProfileConfig,
    shim_dir: Path,
) -> str | None:
    """The executable a profile launches — the user's shell for `kind: shell`."""
    if meta.kind == "shell":
        return profile.binary_path or _shell_path()
    return _resolve_binary_path(meta.command, profile.binary_path, shim_dir)


def _shell_path() -> str | None:
    if os.name == "nt":
        return shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    configured = os.environ.get("SHELL")
    if configured and Path(configured).exists():
        return configured
    return shutil.which("bash") or shutil.which("sh")


def _missing_binary_message(meta: CliCatalogEntry) -> str:
    if meta.kind == "shell":
        return "could not find a shell to open; set a Binary override first"
    return f"could not resolve `{meta.command}` on PATH; set a Binary override first"


def _reject_shell_shim(meta: CliCatalogEntry) -> None:
    if meta.kind == "shell":
        raise RuntimeError(f"{meta.label} has no PATH shim - use Launch instead")


def _resolve_binary_path(command: str, override: str | None, shim_dir: Path) -> str | None:
    if override:
        candidate = Path(os.path.expandvars(os.path.expanduser(override))).expanduser()
        if candidate.exists():
            return str(candidate.resolve())
    filtered_path = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and not _same_path(entry, shim_dir)
    )
    found = shutil.which(command, path=filtered_path or None)
    if not found:
        return None
    return str(Path(found).resolve())


def _shim_path(command: str) -> Path:
    suffix = ".cmd" if os.name == "nt" else ""
    return proxy_shim_dir() / f"{command}{suffix}"


def _render_cmd_shim(
    *, binary_path: str, env_overrides: dict[str, str], launch_args: tuple[str, ...] = ()
) -> str:
    lines = ["@echo off", "setlocal"]
    lines.extend(f'set "{name}={value}"' for name, value in env_overrides.items())
    args = "".join(f" {_quote_for_cmd(arg)}" for arg in launch_args)
    lines.append(f'"{binary_path}"{args} %*')
    return "\n".join(lines) + "\n"


def _render_shell_shim(
    *, binary_path: str, env_overrides: dict[str, str], launch_args: tuple[str, ...] = ()
) -> str:
    lines = ["#!/usr/bin/env sh"]
    lines.extend(f"export {name}={shlex.quote(value)}" for name, value in env_overrides.items())
    args = "".join(f" {shlex.quote(arg)}" for arg in launch_args)
    lines.append(f'exec {shlex.quote(binary_path)}{args} "$@"')
    return "\n".join(lines) + "\n"


def _render_powershell_launch(
    *,
    binary_path: str | None,
    env_overrides: dict[str, str],
    working_directory: Path,
    title: str,
    launch_args: tuple[str, ...] = (),
) -> str:
    env_commands = " ".join(
        f"$env:{name} = '{_quote_for_powershell(value)}';" for name, value in env_overrides.items()
    )
    args = "".join(f" '{_quote_for_powershell(arg)}'" for arg in launch_args)
    run_command = f"& '{_quote_for_powershell(binary_path)}'{args}" if binary_path else ""
    return (
        f"$Host.UI.RawUI.WindowTitle = '{_quote_for_powershell(title)}'; "
        f"{env_commands} "
        f"Set-Location -LiteralPath '{_quote_for_powershell(str(working_directory))}'; "
        f"{run_command}"
    )


def _render_folder_picker_command(initial_dir: Path) -> str:
    return (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$dialog.Description = 'Choose CLI working folder'; "
        "$dialog.UseDescriptionForTitle = $true; "
        f"$dialog.SelectedPath = '{_quote_for_powershell(str(initial_dir))}'; "
        "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
        "  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "  Write-Output $dialog.SelectedPath; "
        "}"
    )


def _render_shell_launch_command(
    *,
    binary_path: str,
    env_overrides: dict[str, str],
    working_directory: Path,
    launch_args: tuple[str, ...] = (),
) -> str:
    exports = " ".join(
        f"export {name}={shlex.quote(value)};" for name, value in env_overrides.items()
    )
    args = "".join(f" {shlex.quote(arg)}" for arg in launch_args)
    return (
        f"cd {shlex.quote(str(working_directory))}; {exports} exec {shlex.quote(binary_path)}{args}"
    )


def _launch_posix_terminal(
    *,
    binary_path: str,
    env_overrides: dict[str, str],
    working_directory: Path,
    title: str,
    launch_args: tuple[str, ...] = (),
) -> None:
    command = _render_shell_launch_command(
        binary_path=binary_path,
        env_overrides=env_overrides,
        working_directory=working_directory,
        launch_args=launch_args,
    )
    if sys.platform == "darwin":
        script = (
            'tell application "Terminal"\n'
            f"  do script {json.dumps(command)}\n"
            "  activate\n"
            "end tell\n"
        )
        subprocess.Popen(["osascript", "-e", script], cwd=str(working_directory))
        return

    terminal_commands = [
        ("x-terminal-emulator", ["x-terminal-emulator", "-T", title, "-e", "sh", "-lc", command]),
        ("gnome-terminal", ["gnome-terminal", "--title", title, "--", "sh", "-lc", command]),
        (
            "konsole",
            [
                "konsole",
                "--new-tab",
                "--workdir",
                str(working_directory),
                "-e",
                "sh",
                "-lc",
                command,
            ],
        ),
        (
            "xfce4-terminal",
            ["xfce4-terminal", "--title", title, "--command", f"sh -lc {shlex.quote(command)}"],
        ),
        ("xterm", ["xterm", "-T", title, "-e", "sh", "-lc", command]),
    ]
    for executable, argv in terminal_commands:
        if shutil.which(executable):
            subprocess.Popen(argv, cwd=str(working_directory))
            return
    raise RuntimeError("no supported terminal emulator found on PATH")


def _choose_directory_tk(initial_dir: str | None = None) -> str | None:
    selected_path = _resolve_working_directory(initial_dir) or Path.home().resolve()
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise RuntimeError("folder picker requires tkinter on macOS/Linux") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        path = filedialog.askdirectory(
            initialdir=str(selected_path),
            title="Choose CLI working folder",
        )
    finally:
        root.destroy()
    return path or None


def _powershell_path() -> str:
    shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not shell:
        raise RuntimeError("PowerShell was not found on PATH")
    return shell


def _quote_for_powershell(value: str) -> str:
    return value.replace("'", "''")


def _quote_for_cmd(value: str) -> str:
    """Quote one argv item for a .cmd shim.

    Our launch args carry embedded double quotes (`-c openai_base_url="..."`),
    which cmd would strip on the way through — doubling them keeps the inner
    quotes intact for the child, which needs them to parse the value as TOML.
    """
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _resolve_working_directory(override: str | None) -> Path | None:
    candidate = override or os.getcwd()
    path = Path(os.path.expandvars(os.path.expanduser(candidate))).expanduser()
    if not path.exists() or not path.is_dir():
        return None
    return path.resolve()


def _is_supported() -> bool:
    return (os.name == "nt" and winreg is not None) or os.name == "posix"


def _require_supported() -> None:
    if not _is_supported():
        raise RuntimeError("automatic CLI proxy install is not supported on this platform")


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return str(left) == str(right)


def _path_contains_user_entry(path: Path) -> bool:
    return any(entry and _same_path(entry, path) for entry in _read_user_path().split(os.pathsep))


def _add_user_path_entry(path: Path) -> None:
    current = [entry for entry in _read_user_path().split(os.pathsep) if entry]
    if any(_same_path(entry, path) for entry in current):
        _prepend_process_path(path)
        return
    updated = [str(path), *current]
    _write_user_path(os.pathsep.join(updated))
    _prepend_process_path(path)


def _remove_user_path_entry(path: Path) -> None:
    current = [entry for entry in _read_user_path().split(os.pathsep) if entry]
    updated = [entry for entry in current if not _same_path(entry, path)]
    _write_user_path(os.pathsep.join(updated))
    _remove_process_path(path)


def _read_user_path() -> str:
    _require_supported()
    if os.name != "nt":
        return os.pathsep.join(_read_posix_managed_path_entries())
    assert winreg is not None
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_ENV_KEY, 0, winreg.KEY_READ) as key:
        try:
            value, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return ""
    return str(value)


def _write_user_path(value: str) -> None:
    _require_supported()
    if os.name != "nt":
        _write_posix_managed_path_entries([entry for entry in value.split(os.pathsep) if entry])
        return
    assert winreg is not None
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        _WINDOWS_ENV_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, value)
    _broadcast_environment_change()


def _posix_profile_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / ".zprofile"
    shell = Path(os.environ.get("SHELL", "")).name
    if shell == "zsh":
        return Path.home() / ".zprofile"
    if shell == "bash":
        return Path.home() / ".bashrc"
    return Path.home() / ".profile"


def _read_posix_managed_path_entries() -> list[str]:
    profile = _posix_profile_path()
    if not profile.exists():
        return []
    text = profile.read_text(encoding="utf-8")
    begin = text.find(_POSIX_PATH_MARKER_BEGIN)
    end = text.find(_POSIX_PATH_MARKER_END)
    if begin == -1 or end == -1 or end < begin:
        return []
    block = text[begin:end]
    entries: list[str] = []
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("export PATH="):
            value = line.removeprefix("export PATH=").strip().strip('"')
            for entry in value.split(os.pathsep):
                if entry and entry != "$PATH":
                    entries.append(entry)
    return entries


def _write_posix_managed_path_entries(entries: list[str]) -> None:
    profile = _posix_profile_path()
    text = profile.read_text(encoding="utf-8") if profile.exists() else ""
    begin = text.find(_POSIX_PATH_MARKER_BEGIN)
    end = text.find(_POSIX_PATH_MARKER_END)
    if begin != -1 and end != -1 and end > begin:
        end += len(_POSIX_PATH_MARKER_END)
        text = text[:begin].rstrip() + "\n" + text[end:].lstrip()

    if entries:
        quoted_path = os.pathsep.join([*entries, "$PATH"])
        block = (
            f'{_POSIX_PATH_MARKER_BEGIN}\nexport PATH="{quoted_path}"\n{_POSIX_PATH_MARKER_END}\n'
        )
        text = (text.rstrip() + "\n\n" + block).lstrip()

    profile.write_text(text, encoding="utf-8")


def _broadcast_environment_change() -> None:
    user32 = cast(Any, ctypes).windll.user32
    user32.SendMessageTimeoutW(
        _HWND_BROADCAST,
        _WM_SETTINGCHANGE,
        0,
        "Environment",
        _SMTO_ABORTIFHUNG,
        5000,
        None,
    )


def _prepend_process_path(path: Path) -> None:
    current = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    if any(_same_path(entry, path) for entry in current):
        return
    os.environ["PATH"] = os.pathsep.join([str(path), *current])


def _remove_process_path(path: Path) -> None:
    current = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    filtered = [entry for entry in current if not _same_path(entry, path)]
    os.environ["PATH"] = os.pathsep.join(filtered)
