"""Typer entrypoint.

Stage Э2 wires up `serve` to actually launch the FastAPI server so the popup
can be opened in a browser. `run` (wraps a target CLI) lands in Э5.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import secrets
import shutil
import socket
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import typer
import uvicorn
from dotenv import find_dotenv, load_dotenv
from rich.console import Console

from voice_copilot import __version__
from voice_copilot.adapters import ClaudeCodeAdapter, CodexAdapter, PtyAdapter
from voice_copilot.adapters.base import CLIAdapter
from voice_copilot.adapters.hooks import HookAdapter
from voice_copilot.alias_install import ensure_vc_alias
from voice_copilot.audio import AudioHub, TTSDriver
from voice_copilot.commentator import Commentator
from voice_copilot.commentator.provider_select import (
    commentator_status_text,
    resolve_commentator_provider,
    resolve_supervisor,
    supervisor_status_text,
)
from voice_copilot.companion import setup as setup_flow
from voice_copilot.companion.center import CommandCenter
from voice_copilot.companion.commands import WORDS, Command, parse, say
from voice_copilot.companion.hook_forward import apply_switch, run_command
from voice_copilot.companion.runtime import (
    NO_HOOKS_ENV,
    RuntimeInfo,
    clear_runtime,
    forget_old_session_switches,
    language,
    read_runtime,
    write_runtime,
)
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig, Config, load_config
from voice_copilot.dialog import DialogManager
from voice_copilot.focus import FocusRouter
from voice_copilot.hotkeys import HotkeyService, default_bindings
from voice_copilot.net import free_port, wait_for_port

# Side-effect imports register the providers in the registry.
from voice_copilot.providers import llm as _llm  # noqa: F401
from voice_copilot.providers import registry as provider_registry
from voice_copilot.providers import stt as _stt  # noqa: F401
from voice_copilot.providers import tts as _tts  # noqa: F401
from voice_copilot.proxy.chat_commands import ChatCommands
from voice_copilot.proxy.cli_shims import ResolvedCli, resolve_cli_for_vc
from voice_copilot.proxy.server import (
    base_urls_for,
    build_proxy_server,
    provider_has_narration,
)
from voice_copilot.proxy.session import SessionRegistry
from voice_copilot.tray import TrayService
from voice_copilot.web.demo import run_demo
from voice_copilot.web.hooks_api import register_hook_routes
from voice_copilot.web.server import ManagedServer, create_app

# Make our own loggers visible. Set VOICE_COPILOT_LOG=DEBUG for the noisy view.
logging.basicConfig(
    level=os.environ.get("VOICE_COPILOT_LOG", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = typer.Typer(
    name="voice-copilot",
    help="Voice pair-programmer for LLM coding CLIs.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

_KNOWN_SUBCOMMANDS = {
    "version",
    "serve",
    "run",
    "proxy",
    "config",
    "vc",
    "companion",
    "switch",
    "command",
    "setup",
}

#: How long an autostarted companion waits after its last session ended.
_COMPANION_IDLE_EXIT_S = 15 * 60.0
#: The same with sessions still open: they crashed without saying goodbye,
#: or the user has simply been away, so this is deliberately generous.
_COMPANION_ORPHAN_EXIT_S = 6 * 60 * 60.0


def _normalize_argv(argv: list[str]) -> list[str]:
    """Let `voice-copilot <name>` work without typing `vc` first.

    Rewrites to `voice-copilot vc <name> ...` whenever the first argument
    isn't a flag or one of our own subcommands. The words a chat takes work here
    too, so `vc off` means the same in a shell as in a CLI.
    """
    if len(argv) == 1 or argv[1].lower() in WORDS:
        return [argv[0], "command", *argv[1:]]
    if len(argv) < 2:
        return argv
    first = argv[1]
    if first.startswith("-") or first in _KNOWN_SUBCOMMANDS:
        return argv
    return [argv[0], "vc", *argv[1:]]


def main() -> None:
    # Answers may be in Russian, and a pipe on Windows defaults to a code page that has none.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")
    # Keys from a `.env` next to where you run voice-copilot (see .env.example).
    # Shell exports win: nothing already in the environment is overridden.
    load_dotenv(find_dotenv(usecwd=True))
    ensure_vc_alias()
    sys.argv[:] = _normalize_argv(sys.argv)
    app()


@app.command()
def version() -> None:
    """Print version and exit."""
    console.print(f"voice-copilot {__version__}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", envvar="VOICE_COPILOT_HOST"),
    port: int = typer.Option(8765, envvar="VOICE_COPILOT_PORT"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
    demo: bool = typer.Option(False, "--demo", help="Emit synthetic events so you can see the UI."),
    hotkeys: bool = typer.Option(True, "--hotkeys/--no-hotkeys"),
    tray: bool = typer.Option(True, "--tray/--no-tray"),
    proxy: bool = typer.Option(
        True,
        "--proxy/--no-proxy",
        help="Start the reverse-proxy too, so CLIs launched from the UI can connect immediately.",
    ),
    proxy_port: int = typer.Option(8766, "--proxy-port"),
) -> None:
    """Start the voice-copilot server, with the standalone proxy enabled by default."""
    if proxy:
        asyncio.run(
            _proxy_only(
                host=host,
                port=port,
                proxy_port=proxy_port,
                open_browser=open_browser,
                enable_hotkeys=hotkeys,
                enable_tray=tray,
            )
        )
        return
    asyncio.run(
        _serve(
            host=host,
            port=port,
            open_browser=open_browser,
            demo=demo,
            enable_hotkeys=hotkeys,
            enable_tray=tray,
        )
    )


@app.command()
def run(
    target: str = typer.Argument(..., help="Target CLI to wrap: claude | codex | pty"),
    prompt: str = typer.Option(None, "--prompt", "-p", help="Initial prompt for the agent."),
    host: str = typer.Option("127.0.0.1", envvar="VOICE_COPILOT_HOST"),
    port: int = typer.Option(8765, envvar="VOICE_COPILOT_PORT"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
    hotkeys: bool = typer.Option(True, "--hotkeys/--no-hotkeys"),
    tray: bool = typer.Option(True, "--tray/--no-tray"),
    binary: str = typer.Option(None, "--binary", help="Override CLI binary path."),
    proxy: bool = typer.Option(
        False,
        "--proxy/--no-proxy",
        help="Route the child CLI's API traffic through our reverse-proxy "
        "so we can narrate `thinking` blocks.",
    ),
    proxy_port: int = typer.Option(8766, "--proxy-port"),
) -> None:
    """Wrap TARGET CLI, narrate its events, and expose the voice popup."""
    env = base_urls_for(host, proxy_port) if proxy else None
    builder: Callable[[EventBus], CLIAdapter]

    if target == "claude":
        builder = lambda bus: ClaudeCodeAdapter(  # noqa: E731
            bus,
            binary=binary or "claude",
            env=env,
            suppress_llm_events=proxy,
        )
    elif target == "codex":
        builder = lambda bus: CodexAdapter(  # noqa: E731
            bus,
            binary=binary or "codex",
            env=env,
            suppress_llm_events=proxy,
        )
    else:
        console.print(
            f"[red]target {target!r} not supported yet.[/red] "
            f"Supported: claude, codex. PTY fallback will come later."
        )
        raise typer.Exit(code=2)
    asyncio.run(
        _run_with_adapter(
            build_adapter=builder,
            prompt=prompt,
            host=host,
            port=port,
            open_browser=open_browser,
            enable_hotkeys=hotkeys,
            enable_tray=tray,
            enable_proxy=proxy,
            proxy_port=proxy_port,
        )
    )


@app.command()
def proxy(
    host: str = typer.Option("127.0.0.1", envvar="VOICE_COPILOT_HOST"),
    port: int = typer.Option(8765, envvar="VOICE_COPILOT_PORT"),
    proxy_port: int = typer.Option(8766, "--proxy-port"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
    hotkeys: bool = typer.Option(True, "--hotkeys/--no-hotkeys"),
    tray: bool = typer.Option(True, "--tray/--no-tray"),
) -> None:
    """Run proxy + web + commentator + TTS. Point any CLI at the shown BASE_URL.

    Works with any CLI that respects `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL`:
    Claude Code, Codex, aider, opencode, Cline, and so on. The popup shows one
    entry per connected client — pick which one to narrate.
    """
    asyncio.run(
        _proxy_only(
            host=host,
            port=port,
            proxy_port=proxy_port,
            open_browser=open_browser,
            enable_hotkeys=hotkeys,
            enable_tray=tray,
        )
    )


@app.command()
def config() -> None:
    """Print the resolved config path. For editing, open the /settings page."""
    from voice_copilot.core.config import config_path, proxy_cli_config_path

    main_path = config_path()
    console.print(f"main config: {main_path}")
    console.print(f"proxy cli config: {proxy_cli_config_path(main_path)}")


@app.command()
def companion(
    host: str = typer.Option("127.0.0.1", envvar="VOICE_COPILOT_HOST"),
    port: int = typer.Option(8765, "--port", help="Panel port; a free one is used if it's taken."),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
    hotkeys: bool = typer.Option(True, "--hotkeys/--no-hotkeys"),
    tray: bool = typer.Option(True, "--tray/--no-tray"),
    autostarted: bool = typer.Option(
        False,
        "--autostarted",
        hidden=True,
        help="Started by a plugin hook: no console to log to, and exit when idle.",
    ),
) -> None:
    """Run in the background for CLIs that have the Voice Copilot plugin.

    You rarely need this by hand: the plugin starts it with the first session.
    """
    asyncio.run(_run_companion(host, port, open_browser, hotkeys, tray, autostarted))


@app.command()
def switch(
    action: str = typer.Argument(..., help="on, off or status"),
    by_agent: bool = typer.Option(False, "--by-agent", hidden=True),
    session: str = typer.Option("", "--session", hidden=True),
) -> None:
    """Turn the plugin's narration on or off: for agents, whose skill says to run this.

    For people there is `vc off`, in a terminal or in a CLI's chat.
    """
    if action not in ("on", "off", "status"):
        raise typer.BadParameter("expected on, off or status")
    text = apply_switch(action, by_agent=by_agent, session_id=session or None)
    console.print(text, markup=False, soft_wrap=True)


@app.command(
    name="command",
    hidden=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def chat_word(words: list[str] = typer.Argument(None)) -> None:  # noqa: B008
    """The words a CLI's chat takes, typed in a terminal: `vc off`, `vc speed 1.5`."""
    command = parse(" ".join(["vc", *(words or [])]))
    if command is None:
        console.print(say("menu", language()), markup=False, soft_wrap=True)
        raise typer.Exit(2)
    answer = run_command(command)
    console.print(answer.get("text"), markup=False, soft_wrap=True)
    if command.verb == "help":
        console.print("\nvc <cli>: launch a CLI through the proxy. vc --help: everything else.")


@app.command()
def setup(
    yes: bool = typer.Option(False, "--yes", "-y", help="Answer yes to everything."),
    test: bool = typer.Option(True, "--test/--no-test", help="Play a test line at the end."),
) -> None:
    """Connect every CLI on this machine, in one go."""
    root = setup_flow.bundle_root()
    codex_ready = False
    for target in setup_flow.TARGETS:
        if shutil.which(target.binary) is None:
            console.print(f"[dim]{target.label}: not installed, skipped[/dim]")
            continue
        ok, detail = target.install(root)
        mark = "[green]connected[/green]" if ok else f"[red]failed[/red] ({detail})"
        console.print(f"{target.label}: {mark}")
        codex_ready = codex_ready or (ok and target.binary == "codex")
    if codex_ready:
        if yes or typer.confirm("Codex runs a plugin's hooks only once trusted. Trust them now?"):
            ok, detail = setup_flow.trust_codex_hooks(cwd=os.getcwd())
            console.print(f"Codex hooks: {detail}")
        else:
            console.print("Codex hooks: run /hooks in Codex once and trust them.")
    others = [name for name in setup_flow.PROXY_ONLY if shutil.which(name)]
    if others:
        names = ", ".join(f"vc {name}" for name in others)
        console.print(f"Through the proxy, with the same commands: {names}")
    if not (yes or typer.confirm("Turn Voice Copilot on now?", default=True)):
        console.print("Later, type vc on in any of them.")
        return
    console.print(apply_switch("on"), markup=False, soft_wrap=True)
    if test:
        console.print(run_command(Command("test")).get("text"), markup=False, soft_wrap=True)
    console.print("In any connected CLI, type vc for the list of commands.")


@app.command(name="vc")
def vc_launch(
    name: str = typer.Argument(
        ..., help="CLI to launch and narrate, e.g. claude, codex, opencode."
    ),
    cli_args: list[str] = typer.Argument(  # noqa: B008
        None, help="Arguments forwarded to the target CLI, after `--`."
    ),
    host: str = typer.Option("127.0.0.1", envvar="VOICE_COPILOT_HOST"),
    port: int = typer.Option(0, "--port", help="Panel port. 0 picks a free port automatically."),
    proxy_port: int = typer.Option(
        0, "--proxy-port", help="Proxy port. 0 picks a free port automatically."
    ),
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


def _server_app_state(server: uvicorn.Server) -> Any:
    return cast(Any, server.config.app).state


def _start_servers(servers: list[uvicorn.Server]) -> list[asyncio.Task[Any]]:
    return [asyncio.create_task(s.serve(), name="uvicorn") for s in servers]


async def _await_shutdown(
    servers: list[uvicorn.Server],
    server_tasks: list[asyncio.Task[Any]],
    extra_tasks: list[asyncio.Task[Any]],
    *,
    hotkey_svc: HotkeyService | None = None,
    tray_svc: TrayService | None = None,
    cleanup: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Wait for tasks until Ctrl+C, then shut down cleanly.

    On interrupt the uvicorn servers are asked to exit gracefully via
    ``should_exit`` (their lifespan unwinds with no traceback) while the other
    background tasks are cancelled.
    """
    all_tasks = [*server_tasks, *extra_tasks]
    try:
        # asyncio.wait (unlike gather) does NOT cancel its tasks when this — the
        # main task — is cancelled by asyncio.run()'s Ctrl+C handling. That lets
        # us unwind the uvicorn servers gracefully via should_exit below instead
        # of hard-cancelling their lifespan mid-flight (which logs a traceback).
        await asyncio.wait(all_tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for s in servers:
            s.should_exit = True
        for t in extra_tasks:
            t.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        if cleanup is not None:
            await cleanup()
        if hotkey_svc is not None:
            hotkey_svc.stop()
        if tray_svc is not None:
            tray_svc.stop()


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
        # The wrapped CLI first: whatever it left running (codex's sub-agents
        # outlive `codex exec`) holds connections through the proxy, and the
        # servers below drain faster with those clients gone.
        if cleanup is not None:
            await cleanup()
        for s in servers:
            s.should_exit = True
        for t in extra_tasks:
            t.cancel()
        await asyncio.gather(*server_tasks, *extra_tasks, return_exceptions=True)
        if hotkey_svc is not None:
            hotkey_svc.stop()
        if tray_svc is not None:
            tray_svc.stop()


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
    quiet_logging: bool = False,
) -> tuple[uvicorn.Server, HotkeyService | None, TrayService | None, Config, AudioHub]:
    cfg = load_config()
    # Everything VC spawns inherits this: a CLI we already narrate through the
    # proxy, and the narrator's own `claude -p`, must not report to the plugin
    # endpoint as well, or VC would end up narrating itself.
    os.environ[NO_HOOKS_ENV] = "1"

    hub = AudioHub()
    stt_provider = None
    # Voice input (mic -> STT -> inject) is behind a config flag while the flow
    # is being reworked; with it off we never build an STT provider at all.
    if cfg.voice_input.enabled:
        try:
            stt_provider = provider_registry.build("stt", cfg.stt.name, dict(cfg.stt.options))
        except Exception as e:
            console.print(f"[yellow]STT provider unavailable: {e}[/yellow]")

    fast_app = create_app(
        bus,
        cfg,
        audio_hub=hub,
        stt_provider=stt_provider,
        sessions=sessions,
        proxy_port=proxy_port,
    )
    # quiet_logging (used by `vc`): pass log_config=None so uvicorn does NOT
    # install its own stderr handlers — its loggers then propagate to the root
    # logger, which `_run_vc` has redirected to a file. Otherwise uvicorn would
    # keep printing to the console the child terminal now owns.
    if quiet_logging:
        uv_config = uvicorn.Config(
            fast_app,
            host=host,
            port=port,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=3,
        )
    else:
        uv_config = uvicorn.Config(
            fast_app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
            timeout_graceful_shutdown=3,
        )
    server = ManagedServer(uv_config)

    loop = asyncio.get_running_loop()
    hotkey_svc: HotkeyService | None = None
    tray_svc: TrayService | None = None

    if enable_hotkeys:
        try:
            hotkey_svc = HotkeyService(
                bus,
                loop,
                default_bindings(cfg.hotkeys, voice_input=cfg.voice_input.enabled),
                is_focused=is_focused,
            )
            hotkey_svc.start()
        except Exception as e:
            console.print(f"[yellow]hotkeys unavailable: {e}[/yellow]")

    if enable_tray:
        tray_svc = TrayService(host, port)
        tray_svc.start()

    if open_browser:
        import webbrowser

        loop.call_later(0.7, lambda: webbrowser.open(f"http://{host}:{port}/"))

    return server, hotkey_svc, tray_svc, cfg, hub


async def _serve(
    host: str,
    port: int,
    open_browser: bool,
    demo: bool,
    enable_hotkeys: bool,
    enable_tray: bool,
) -> None:
    bus = EventBus()
    server, hotkey_svc, tray_svc, cfg, hub = await _boot(
        bus, host, port, open_browser, enable_hotkeys, enable_tray
    )

    server_tasks = _start_servers([server])
    extra: list[asyncio.Task[Any]] = []
    tts_result = _start_tts_driver(bus, hub, cfg)
    if tts_result is not None:
        extra.append(tts_result[1])
    if demo:
        extra.append(asyncio.create_task(run_demo(bus), name="demo"))
        commentator = Commentator(bus, cfg.commentator, cfg.commentator_language, sessions=None)
        _server_app_state(server).commentator = commentator
        extra.append(asyncio.create_task(commentator.run(), name="commentator"))
    await _await_shutdown([server], server_tasks, extra, hotkey_svc=hotkey_svc, tray_svc=tray_svc)


async def _proxy_only(
    host: str,
    port: int,
    proxy_port: int,
    open_browser: bool,
    enable_hotkeys: bool,
    enable_tray: bool,
) -> None:
    bus = EventBus()
    sessions = SessionRegistry()
    server, hotkey_svc, tray_svc, cfg, hub = await _boot(
        bus,
        host,
        port,
        open_browser,
        enable_hotkeys,
        enable_tray,
        sessions=sessions,
        proxy_port=proxy_port,
    )

    commentator = Commentator(bus, cfg.commentator, cfg.commentator_language, sessions=sessions)
    _server_app_state(server).commentator = commentator
    proxy_server = build_proxy_server(bus, host=host, port=proxy_port, registry=sessions)
    servers = [server, proxy_server]
    server_tasks = _start_servers(servers)
    extra: list[asyncio.Task[Any]] = [
        asyncio.create_task(commentator.run(), name="commentator"),
    ]
    tts_result = _start_tts_driver(bus, hub, cfg)
    if tts_result is not None:
        extra.append(tts_result[1])

    urls = base_urls_for(host, proxy_port)
    console.print("\n[bold green]voice-copilot proxy ready — point your CLI at:[/bold green]")
    for k, v in urls.items():
        console.print(f"  [cyan]{k}[/cyan]=[white]{v}[/white]")
    console.print(
        f'[dim]Example:  ANTHROPIC_BASE_URL={urls["ANTHROPIC_BASE_URL"]} claude -p "hi"[/dim]\n'
    )

    await _await_shutdown(servers, server_tasks, extra, hotkey_svc=hotkey_svc, tray_svc=tray_svc)


async def _run_with_adapter(
    build_adapter: Callable[[EventBus], CLIAdapter],
    prompt: str | None,
    host: str,
    port: int,
    open_browser: bool,
    enable_hotkeys: bool,
    enable_tray: bool,
    enable_proxy: bool = False,
    proxy_port: int = 8766,
) -> None:
    bus = EventBus()
    sessions = SessionRegistry() if enable_proxy else None
    server, hotkey_svc, tray_svc, cfg, hub = await _boot(
        bus,
        host,
        port,
        open_browser,
        enable_hotkeys,
        enable_tray,
        sessions=sessions,
        proxy_port=proxy_port if enable_proxy else None,
    )

    commentator = Commentator(bus, cfg.commentator, cfg.commentator_language, sessions=sessions)
    _server_app_state(server).commentator = commentator
    servers: list[uvicorn.Server] = [server]
    if enable_proxy:
        servers.append(build_proxy_server(bus, host=host, port=proxy_port, registry=sessions))
    server_tasks = _start_servers(servers)
    extra: list[asyncio.Task[Any]] = [
        asyncio.create_task(commentator.run(), name="commentator"),
    ]
    tts_result = _start_tts_driver(bus, hub, cfg)
    if tts_result is not None:
        extra.append(tts_result[1])
    if enable_proxy:
        console.print(
            f"[green]proxy → ANTHROPIC_BASE_URL=http://{host}:{proxy_port}/anthropic  "
            f"OPENAI_BASE_URL=http://{host}:{proxy_port}/openai/v1[/green]"
        )
        # Wait for uvicorn to actually bind before the child CLI uses the URL,
        # so its first request cannot race past the proxy unnarrated.
        if not await wait_for_port(host, proxy_port, timeout=10.0):
            console.print(
                f"[yellow]proxy did not come up on {host}:{proxy_port} in time — "
                f"narration may miss the first request[/yellow]"
            )

    adapter: CLIAdapter = build_adapter(bus)
    dialog = DialogManager(bus, adapter, cfg.dialog)
    _server_app_state(server).dialog = dialog
    extra.append(asyncio.create_task(dialog.run(), name="dialog"))
    try:
        await adapter.start(initial_prompt=prompt)
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

    await _await_shutdown(
        servers,
        server_tasks,
        extra,
        hotkey_svc=hotkey_svc,
        tray_svc=tray_svc,
        cleanup=adapter.stop,
    )


def _port_or_free(host: str, port: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return free_port(host)
    return port


async def _exit_when_idle(adapter: HookAdapter) -> None:
    """Don't leave a background process behind once the sessions are gone."""
    while True:
        await asyncio.sleep(30.0)
        limit = _COMPANION_ORPHAN_EXIT_S if adapter.session_count else _COMPANION_IDLE_EXIT_S
        if adapter.idle_seconds() >= limit:
            return


async def _run_companion(
    host: str,
    port: int,
    open_browser: bool,
    enable_hotkeys: bool,
    enable_tray: bool,
    autostarted: bool,
) -> None:
    running = read_runtime()
    if running is not None and await wait_for_port(running.host, running.port, timeout=0.5):
        console.print(f"[yellow]Already running on {running.host}:{running.port}[/yellow]")
        return
    if autostarted:
        _route_logging_to_file()

    bus = EventBus()
    sessions = SessionRegistry()
    actual_port = _port_or_free(host, port)
    server, hotkey_svc, tray_svc, cfg, hub = await _boot(
        bus,
        host,
        actual_port,
        open_browser,
        enable_hotkeys,
        enable_tray,
        sessions=sessions,
        quiet_logging=autostarted,
    )
    state = _server_app_state(server)
    state.attached = True

    def resolve_commentator(saved: Config) -> tuple[CommentatorConfig, str]:
        # Which CLI to reuse for `auto` narration is only known once one reports in.
        cli = adapter.active_cli
        binary = shutil.which(cli) if cli else None
        commentator_cfg, status = _commentator_resolution(saved, cli, binary)
        attached = f"Attached to {cli}" if cli else "Waiting for a session from the plugin"
        return commentator_cfg, f"{attached}  •  {status}"

    def on_active_cli(_cli: str | None) -> None:
        commentator_cfg, notice = resolve_commentator(state.config)
        commentator.update_config(commentator_cfg, state.config.commentator_language)
        state.launch_notice = notice

    def greeting() -> str:
        return say("connected", state.config.commentator_language)

    adapter = HookAdapter(bus, sessions, on_active_cli=on_active_cli, greeting=greeting)
    commentator_cfg, state.launch_notice = resolve_commentator(cfg)
    commentator = Commentator(bus, commentator_cfg, cfg.commentator_language, sessions=sessions)
    state.commentator = commentator
    state.commentator_resolver = resolve_commentator

    center = CommandCenter(
        bus=bus,
        state=state,
        commentator=commentator,
        adapter=adapter,
        hub=hub,
        panel_url=f"http://{host}:{actual_port}/",
        active_cli=lambda: adapter.active_cli,
        active_session=sessions.get_active_id,
    )
    forget_old_session_switches()
    token = secrets.token_urlsafe(32)
    register_hook_routes(state.app, adapter, token, lambda: commentator.supervisor_mode, center)
    dialog = DialogManager(bus, adapter, cfg.dialog)
    state.dialog = dialog

    server_tasks = _start_servers([server])
    extra: list[asyncio.Task[Any]] = [
        asyncio.create_task(commentator.run(), name="commentator"),
        asyncio.create_task(dialog.run(), name="dialog"),
        asyncio.create_task(center.watch(), name="commands"),
    ]
    tts_result = _start_tts_driver(bus, hub, cfg)
    if tts_result is not None:
        extra.append(tts_result[1])
    # Published only once the port answers, so a hook that finds this file can post.
    if not await wait_for_port(host, actual_port, timeout=10.0):
        logging.getLogger(__name__).warning("companion did not bind %s:%s", host, actual_port)
    write_runtime(RuntimeInfo(host=host, port=actual_port, token=token, pid=os.getpid()))
    if not autostarted:
        console.print(
            f"[bold green]Voice Copilot companion on http://{host}:{actual_port}/[/bold green]"
        )

    idle_task = asyncio.create_task(_exit_when_idle(adapter), name="companion.idle")
    try:
        await _await_vc_shutdown(
            [server],
            server_tasks,
            extra,
            idle_task if autostarted else None,
            hotkey_svc=hotkey_svc,
            tray_svc=tray_svc,
            cleanup=adapter.stop,
        )
    finally:
        idle_task.cancel()
        clear_runtime(token=token)


def _route_logging_to_file() -> Path:
    """Send voice-copilot's own logging to a file instead of the console.

    `vc` hands the terminal to the child CLI, so the parent process must not
    write to that console — interleaved log lines corrupt the child's display
    (scrolling, misplaced cursor). Returns the log path.
    """
    from voice_copilot.core.config import config_path

    log_path = config_path().parent / "vc-session.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    return log_path


# Claude Code hides its Remote Control menu while ANTHROPIC_BASE_URL points at a
# custom host (our proxy). Surface that in the panel so the user doesn't blame vc
# for the missing menu. Kept short — it shares the panel's single-line banner.
_REMOTE_CONTROL_NOTE = (
    "Note: Claude Code hides its Remote Control menu while narration routes "
    "through the proxy — run claude without vc for a session that needs it."
)


def _no_narration_note(provider: str) -> str:
    return (
        f"Heads up: {provider} traffic is proxied but not narrated yet — "
        f"you'll get voice questions in, but no spoken play-by-play out."
    )


def _launch_notice(resolved: ResolvedCli | None, commentator_status: str) -> str:
    """Compose the panel banner shown at launch (status + any caveats)."""
    parts = [commentator_status]
    if resolved is not None:
        if resolved.profile_id == "claude":
            parts.append(_REMOTE_CONTROL_NOTE)
        if not provider_has_narration(resolved.provider):
            parts.append(_no_narration_note(resolved.label))
    return "  •  ".join(parts)


def _not_recognized_note(name: str) -> str:
    return (
        f"'{name}' isn't a recognized CLI — launching without narration. "
        f"Add a proxy_cli.profiles.{name} entry to your config to enable it."
    )


def _apply_commentator_resolution(
    cfg: Config, resolved: ResolvedCli | None
) -> tuple[CommentatorConfig, str]:
    """Return (effective commentator config, panel status) for this launch.

    The returned config is a *copy* with `provider` set to the effective
    provider — the shared `cfg` is left untouched so the runtime `auto`
    provider (which carries an absolute binary path) never round-trips into
    the user's saved config via /api/config.
    """
    cli = resolved.profile_id if resolved is not None else None
    binary = resolved.resolved_binary if resolved is not None else None
    return _commentator_resolution(cfg, cli, binary)


def _commentator_resolution(
    cfg: Config, cli: str | None, binary: str | None
) -> tuple[CommentatorConfig, str]:
    effective = resolve_commentator_provider(cfg.commentator, cli=cli, binary=binary)
    commentator_cfg = cfg.commentator.model_copy(deep=True)
    commentator_cfg.provider = effective
    commentator_cfg.supervisor = resolve_supervisor(cfg.commentator, cli=cli)
    status = commentator_status_text(effective, cli)
    sup_status = supervisor_status_text(commentator_cfg.supervisor)
    if sup_status:
        status = f"{status}  •  {sup_status}"
    return commentator_cfg, status


def _make_commentator_resolver(
    resolved: ResolvedCli | None, name: str
) -> Callable[[Config], tuple[CommentatorConfig, str]]:
    """Bind this launch's resolved CLI so /api/config can redo the resolution.

    A panel save hands the server the *saved* config, whose `commentator.provider`
    block is whatever the user last picked for `api` mode. Feeding that straight
    to the running Commentator would silently replace an `auto` (reuse-the-CLI)
    provider with an unconfigured API one, and every narration afterwards would
    die on a missing key — so the save path re-applies the same resolution the
    launch did.
    """

    def resolve(cfg: Config) -> tuple[CommentatorConfig, str]:
        commentator_cfg, status = _apply_commentator_resolution(cfg, resolved)
        if resolved is not None:
            return commentator_cfg, _launch_notice(resolved, status)
        return commentator_cfg, f"{_not_recognized_note(name)}  •  {status}"

    return resolve


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
    _route_logging_to_file()
    bus = EventBus()
    cfg_for_resolve = load_config()
    focus_router = FocusRouter(
        narrate_only_when_focused=cfg_for_resolve.focus.narrate_only_when_focused
    )
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
        is_focused=lambda: focus_router.current_focus,
        quiet_logging=True,
    )

    resolve_commentator = _make_commentator_resolver(resolved, name)
    commentator_cfg, launch_notice = resolve_commentator(cfg)
    commentator = Commentator(bus, commentator_cfg, cfg.commentator_language, sessions=sessions)
    _server_app_state(server).commentator = commentator
    _server_app_state(server).commentator_resolver = resolve_commentator
    _server_app_state(server).launch_notice = launch_notice
    _server_app_state(server).focus_router = focus_router
    state = _server_app_state(server)
    panel_url = f"http://{host}:{server.config.port}/"
    chat_commands = ChatCommands(
        bus=bus, panel_url=panel_url, language=lambda: state.config.commentator_language
    )
    servers: list[uvicorn.Server] = [server]
    if enable_proxy:
        servers.append(
            build_proxy_server(
                bus,
                host=host,
                port=actual_proxy_port,
                registry=sessions,
                quiet=True,
                commands=chat_commands,
            )
        )
    server_tasks = _start_servers(servers)
    extra: list[asyncio.Task[Any]] = [asyncio.create_task(commentator.run(), name="commentator")]
    tts_result = _start_tts_driver(bus, hub, cfg)
    if tts_result is not None:
        tts_driver, tts_task = tts_result
        extra.append(tts_task)
        focus_router.on_narrate_gate(tts_driver.set_focus_gate)

    focus_router.start()

    # Status goes to the browser panel (via /api/info), not the console — the
    # child CLI owns the terminal, so anything we print there is wiped when the
    # PTY clears the screen on handover.
    if resolved is not None:
        binary = resolved.resolved_binary
        launch_args = list(resolved.launch_args)
        full_env = {**os.environ, **resolved.env_overrides}
        cwd = str(resolved.working_directory) if resolved.working_directory else None
        # Wait for the proxy to bind before the child starts using its base URL.
        # The child owns the terminal here, so a failure goes to the log file
        # (routed by `_route_logging_to_file`), never the console.
        if not await wait_for_port(host, actual_proxy_port, timeout=10.0):
            logging.getLogger(__name__).warning(
                "proxy did not come up on %s:%s in time — first request may be unnarrated",
                host,
                actual_proxy_port,
            )
    else:
        binary = name
        launch_args = []
        full_env = dict(os.environ)
        cwd = None

    adapter = PtyAdapter(bus, [binary, *launch_args, *cli_args], env=full_env, cwd=cwd)
    dialog = DialogManager(bus, adapter, cfg.dialog)
    _server_app_state(server).dialog = dialog
    extra.append(asyncio.create_task(dialog.run(), name="dialog"))
    center = CommandCenter(
        bus=bus,
        state=state,
        commentator=commentator,
        adapter=adapter,
        hub=hub,
        panel_url=panel_url,
        active_cli=lambda: resolved.profile_id if resolved is not None else None,
        active_session=sessions.get_active_id if sessions is not None else lambda: None,
        chat_replies=False,
    )
    chat_commands.center = center
    extra.append(asyncio.create_task(center.watch(), name="commands"))
    if enable_proxy:
        connected = say("connected", state.config.commentator_language)
        state.launch_notice = f"{connected}  •  {state.launch_notice}"
        # A full-screen CLI may wipe this. A plain one, or a run under -p, keeps it.
        console.print(connected, markup=False, soft_wrap=True)

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
        focus_router.stop()
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
    focus_router.stop()
