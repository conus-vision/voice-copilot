"""Typer entrypoint.

Stage Э2 wires up `serve` to actually launch the FastAPI server so the popup
can be opened in a browser. `run` (wraps a target CLI) lands in Э5.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any, cast

import typer
import uvicorn
from rich.console import Console

from voice_copilot import __version__
from voice_copilot.adapters import ClaudeCodeAdapter, CodexAdapter, PtyAdapter
from voice_copilot.adapters.base import CLIAdapter
from voice_copilot.alias_install import ensure_vc_alias
from voice_copilot.audio import AudioHub, TTSDriver
from voice_copilot.commentator import Commentator
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import Config, load_config
from voice_copilot.dialog import DialogManager
from voice_copilot.focus import FocusRouter
from voice_copilot.hotkeys import HotkeyService, default_bindings
from voice_copilot.net import free_port

# Side-effect imports register the providers in the registry.
from voice_copilot.providers import llm as _llm  # noqa: F401
from voice_copilot.providers import registry as provider_registry
from voice_copilot.providers import stt as _stt  # noqa: F401
from voice_copilot.providers import tts as _tts  # noqa: F401
from voice_copilot.proxy.cli_shims import ResolvedCli, resolve_cli_for_vc
from voice_copilot.proxy.server import base_urls_for, build_proxy_server
from voice_copilot.proxy.session import SessionRegistry
from voice_copilot.tray import TrayService
from voice_copilot.web.demo import run_demo
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
    cfg = load_config()

    hub = AudioHub()
    stt_provider = None
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
    uv_config = uvicorn.Config(fast_app, host=host, port=port, log_level="info", access_log=False)
    server = ManagedServer(uv_config)

    loop = asyncio.get_running_loop()
    hotkey_svc: HotkeyService | None = None
    tray_svc: TrayService | None = None

    if enable_hotkeys:
        try:
            hotkey_svc = HotkeyService(
                bus, loop, default_bindings(cfg.hotkeys), is_focused=is_focused
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
        # Give uvicorn a moment to bind before the child CLI tries to use the URL.
        await asyncio.sleep(0.3)

    adapter: CLIAdapter = build_adapter(bus)
    dialog = DialogManager(bus, adapter, cfg.dialog)
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
    )

    commentator = Commentator(bus, cfg.commentator, cfg.commentator_language, sessions=sessions)
    _server_app_state(server).commentator = commentator
    _server_app_state(server).focus_router = focus_router
    servers: list[uvicorn.Server] = [server]
    if enable_proxy:
        servers.append(
            build_proxy_server(bus, host=host, port=actual_proxy_port, registry=sessions)
        )
    server_tasks = _start_servers(servers)
    extra: list[asyncio.Task[Any]] = [asyncio.create_task(commentator.run(), name="commentator")]
    tts_result = _start_tts_driver(bus, hub, cfg)
    if tts_result is not None:
        tts_driver, tts_task = tts_result
        extra.append(tts_task)
        focus_router.on_narrate_gate(tts_driver.set_focus_gate)

    focus_router.start()

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
