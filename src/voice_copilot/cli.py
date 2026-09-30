"""Typer entrypoint.

Stage Э2 wires up `serve` to actually launch the FastAPI server so the popup
can be opened in a browser. `run` (wraps a target CLI) lands in Э5.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import typer
import uvicorn
from rich.console import Console

from voice_copilot import __version__
from voice_copilot.adapters import ClaudeCodeAdapter, CodexAdapter, PtyAdapter
from voice_copilot.adapters.base import CLIAdapter
from voice_copilot.alias_install import ensure_vc_alias
from voice_copilot.audio import AudioHub, TTSDriver
from voice_copilot.commentator import Commentator
from voice_copilot.commentator.provider_select import resolve_for_launch
from voice_copilot.companion import integrations
from voice_copilot.core.bus import EventBus
from voice_copilot.core.child_env import child_env, load_dotenv_for_self
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
from voice_copilot.providers.tts.base import TTSProvider, UnavailableTTS
from voice_copilot.proxy.cli_shims import ResolvedCli, proxy_launch_settings, resolve_cli_for_vc
from voice_copilot.proxy.server import (
    base_urls_for,
    build_proxy_server,
    provider_has_narration,
    proxy_bind_host,
)
from voice_copilot.proxy.session import SessionRegistry
from voice_copilot.tray import TrayService
from voice_copilot.web.access import PanelAccess
from voice_copilot.web.demo import run_demo
from voice_copilot.web.server import ManagedServer, create_app

# Make our own loggers visible. Set VOICE_COPILOT_LOG=DEBUG for the noisy view.
_LOG_LEVEL = os.environ.get("VOICE_COPILOT_LOG", "INFO").strip().upper()
logging.basicConfig(
    # `debug` works as well as `DEBUG`; an unknown name falls back to INFO
    # rather than crashing the import.
    level=_LOG_LEVEL if _LOG_LEVEL in logging.getLevelNamesMapping() else "INFO",
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = typer.Typer(
    name="voice-copilot",
    help="Voice pair-programmer for LLM coding CLIs.",
    no_args_is_help=True,
    add_completion=False,
)
#: The Typer app itself, for reading its commands even where tests replace `app`.
_TYPER_APP = app
console = Console()


def _known_subcommands() -> set[str]:
    """Names of our own subcommands, read from the app so a new one is never missed."""
    names = set()
    for command in _TYPER_APP.registered_commands:
        if command.name:
            names.add(command.name)
        elif command.callback is not None:
            names.add(command.callback.__name__.replace("_", "-"))
    return names


def _normalize_argv(argv: list[str]) -> list[str]:
    """Let `voice-copilot <name>` work without typing `vc` first.

    Rewrites to `voice-copilot vc <name> ...` whenever the first argument
    isn't a flag or one of our own subcommands.
    """
    if len(argv) < 2:
        return argv
    first = argv[1]
    if first.startswith("-") or first in _known_subcommands():
        return argv
    return [argv[0], "vc", *argv[1:]]


def main() -> None:
    # Keys from a `.env` next to where you run voice-copilot (see .env.example).
    # Shell exports win: nothing already in the environment is overridden.
    # They are for voice-copilot itself; wrapped CLIs never see them.
    load_dotenv_for_self()
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
                demo=demo,
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
    target: str = typer.Argument(..., help="Target CLI to wrap: claude | codex"),
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
    env: dict[str, str] | None = None
    proxy_args: list[str] = []
    if proxy and target in ("claude", "codex"):
        # The same routing `vc` uses: codex ignores OPENAI_BASE_URL for model
        # traffic and needs its endpoint as a `-c openai_base_url=…` flag, or
        # nothing reaches the proxy while the adapter's own events are muted.
        overrides, launch_args = proxy_launch_settings(
            target, load_config(), host=proxy_bind_host(), port=proxy_port
        )
        env, proxy_args = overrides, list(launch_args)
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
            extra_args=proxy_args,
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
def integrate(
    cli: str | None = typer.Argument(
        None,
        help="CLI to connect: claude, pi, opencode, hermes, codex, gemini, qwen, copilot, "
        "grok, droid, openhands, kimi. Leave it out to list them.",
    ),
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove what an install added."),
    port: int = typer.Option(
        8765,
        "--port",
        envvar="VOICE_COPILOT_PORT",
        help="Panel port the Claude Code plugin reports to (the port of `voice-copilot serve`).",
    ),
    print_only: bool = typer.Option(
        False, "--print", help="Show the steps to do it by hand instead of changing anything."
    ),
) -> None:
    """Connect a coding CLI to Voice Copilot through its own plugin or hook system.

    Once connected, the CLI reports what it does to a running Voice Copilot
    (`voice-copilot serve`, or any `vc` session) and gets narrated without the
    proxy. `vc claude` and `vc pi` load their plugin for the session on their
    own; this command is for running the CLI directly.
    """
    if cli is None:
        _print_integrations(port)
        return
    try:
        integration = integrations.get(cli.lower())
    except KeyError:
        known = ", ".join(i.id for i in integrations.all_integrations())
        console.print(f"[red]No integration for {cli!r}.[/red] Known: {known}")
        raise typer.Exit(2) from None
    if print_only:
        integration.prepare(port=port)
        console.print(f"[bold]{integration.label}[/bold]: {integration.method}")
        for step in integration.steps(port=port):
            console.print(step, markup=False, highlight=False)
        return
    try:
        report = integration.uninstall() if uninstall else integration.install(port=port)
    except integrations.IntegrationError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"[green]{report}[/green]")
    if not uninstall:
        for step in integration.after_install:
            console.print(f"[bold]Next:[/bold] {step.format(port=port)}", highlight=False)
        if integration.notes:
            console.print(f"[dim]{integration.notes}[/dim]")
        console.print(
            "[dim]Start `voice-copilot serve` and use the CLI as usual; "
            "the panel's Integrations section shows when it connects.[/dim]"
        )


def _print_integrations(port: int) -> None:
    from rich.table import Table

    table = Table(title="Voice Copilot integrations", show_lines=False)
    for column in ("CLI", "Id", "How", "Installed", "Controls", "Tested"):
        table.add_column(column)
    for item in integrations.describe_all(port=port):
        table.add_row(
            item["label"],
            item["id"],
            item["method"] + (" (auto in vc)" if item["session_auto"] else ""),
            "yes" if item["installed"] else "no",
            ", ".join(item["controls"]),
            "yes" if item["verified"] else "docs only",
        )
    console.print(table)
    console.print(
        "Connect one with `voice-copilot integrate <id>`; `--print` shows the manual steps, "
        "`--uninstall` removes it."
    )


@app.command()
def config() -> None:
    """Print the resolved config path. For editing, open the /settings page."""
    from voice_copilot.core.config import config_path, proxy_cli_config_path

    main_path = config_path()
    console.print(f"main config: {main_path}")
    console.print(f"proxy cli config: {proxy_cli_config_path(main_path)}")


@app.command(
    name="vc",
    # `vc claude --resume` hands --resume to claude; vc's own options still
    # parse as vc's, and `--` still ends them explicitly.
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def vc_launch(
    name: str = typer.Argument(
        ..., help="CLI to launch and narrate, e.g. claude, codex, opencode."
    ),
    cli_args: list[str] = typer.Argument(  # noqa: B008
        None,
        help="Arguments forwarded to the target CLI. Put `--` before any that "
        "share a name with vc's own options (--port, --open, ...).",
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
    bus: EventBus, hub: AudioHub, cfg: Config, server: uvicorn.Server
) -> tuple[TTSDriver, asyncio.Task[None]]:
    tts: TTSProvider
    try:
        tts = provider_registry.build("tts", cfg.tts.name, dict(cfg.tts.options))
    except Exception as e:
        console.print(f"[yellow]TTS provider unavailable: {e}[/yellow]")
        # Each line says why in the panel, and a voice saved there takes over.
        tts = UnavailableTTS(f"voice {cfg.tts.name!r} could not start: {e}")
    driver = TTSDriver(bus, hub, tts, cfg.commentator_language)
    _server_app_state(server).tts_driver = driver
    return driver, asyncio.create_task(driver.run(), name="tts.driver")


def _server_app_state(server: uvicorn.Server) -> Any:
    return cast(Any, server.config.app).state


def _start_companion_control(server: uvicorn.Server) -> asyncio.Task[None]:
    """Let plugin sessions take the user's pause, interrupt and voice messages.

    Only where no DialogManager runs (``serve``): with ``vc`` the terminal
    wrapper already owns those controls.
    """
    companion = _server_app_state(server).companion
    companion.owns_dialog = True
    return asyncio.create_task(companion.run(), name="companion.control")


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
    # Only the wrapped CLI or a server ending ends the session. A background
    # task (narrator, TTS) that dies is logged; it must not take the user's
    # agent down with it.
    wait_tasks = list(server_tasks)
    if child_exit_task is not None:
        wait_tasks.append(child_exit_task)
    for task in extra_tasks:
        task.add_done_callback(_log_task_failure)
    try:
        await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        # The wrapped CLI first: whatever it left running (codex's sub-agents
        # outlive `codex exec`) holds connections through the proxy, and the
        # servers below drain faster with those clients gone.
        if cleanup is not None:
            try:
                await cleanup()
            except Exception:
                logging.getLogger(__name__).exception("stopping the wrapped CLI failed")
        for s in servers:
            s.should_exit = True
        for t in extra_tasks:
            t.cancel()
        await asyncio.gather(*server_tasks, *extra_tasks, return_exceptions=True)
        if hotkey_svc is not None:
            hotkey_svc.stop()
        if tray_svc is not None:
            tray_svc.stop()


def _log_task_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logging.getLogger(__name__).error(
            "background task %s failed", task.get_name(), exc_info=task.exception()
        )


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
    access = PanelAccess()

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
        bind_host=host,
        panel_port=port,
        access=access,
    )
    network_url = access.network_url(host, port)
    if network_url:
        message = (
            f"Panel for other devices: {network_url}\n"
            "Anyone with this link can control the agent; keep it private."
        )
        if quiet_logging:
            logging.getLogger(__name__).info(message)
        else:
            console.print(message, highlight=False, soft_wrap=True)
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
            fast_app.state.hotkeys = hotkey_svc
        except Exception as e:
            console.print(f"[yellow]hotkeys unavailable: {e}[/yellow]")

    if enable_tray:
        # The tray's Quit cancels the main task, which every entrypoint's
        # shutdown path already treats like Ctrl+C.
        main_task = asyncio.current_task()

        def quit_from_tray() -> None:
            if main_task is not None:
                loop.call_soon_threadsafe(main_task.cancel)

        tray_svc = TrayService(
            host, port, on_quit=quit_from_tray, url=access.browser_url(host, port)
        )
        tray_svc.start()

    if open_browser:
        import webbrowser

        loop.call_later(0.7, lambda: webbrowser.open(access.browser_url(host, port)))

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
    extra: list[asyncio.Task[Any]] = [_start_companion_control(server)]
    extra.append(_start_tts_driver(bus, hub, cfg, server)[1])
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
    demo: bool = False,
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
    proxy_server = build_proxy_server(
        bus, host=proxy_bind_host(), port=proxy_port, registry=sessions
    )
    servers = [server, proxy_server]
    server_tasks = _start_servers(servers)
    extra: list[asyncio.Task[Any]] = [
        asyncio.create_task(commentator.run(), name="commentator"),
        _start_companion_control(server),
    ]
    extra.append(_start_tts_driver(bus, hub, cfg, server)[1])
    if demo:
        extra.append(asyncio.create_task(run_demo(bus), name="demo"))

    urls = base_urls_for(proxy_bind_host(), proxy_port)
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
        servers.append(
            build_proxy_server(bus, host=proxy_bind_host(), port=proxy_port, registry=sessions)
        )
    server_tasks = _start_servers(servers)
    extra: list[asyncio.Task[Any]] = [
        asyncio.create_task(commentator.run(), name="commentator"),
    ]
    extra.append(_start_tts_driver(bus, hub, cfg, server)[1])
    if enable_proxy:
        phost = proxy_bind_host()
        console.print(
            f"[green]proxy → ANTHROPIC_BASE_URL=http://{phost}:{proxy_port}/anthropic  "
            f"OPENAI_BASE_URL=http://{phost}:{proxy_port}/openai/v1[/green]"
        )
        # Wait for uvicorn to actually bind before the child CLI uses the URL,
        # so its first request cannot race past the proxy unnarrated.
        if not await wait_for_port(phost, proxy_port, timeout=10.0):
            console.print(
                f"[yellow]proxy did not come up on {phost}:{proxy_port} in time — "
                f"narration may miss the first request[/yellow]"
            )

    adapter: CLIAdapter = build_adapter(bus)
    dialog = DialogManager(bus, adapter, cfg.dialog)
    _server_app_state(server).dialog = dialog
    extra.append(asyncio.create_task(dialog.run(), name="dialog"))
    try:
        await adapter.start(initial_prompt=prompt)
    except (RuntimeError, OSError) as e:
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


def _launch_notice(
    resolved: ResolvedCli | None, commentator_status: str, *, plugin_narrates: bool = False
) -> str:
    """Compose the panel banner shown at launch (status + any caveats)."""
    parts = [commentator_status]
    if resolved is not None and not plugin_narrates:
        if resolved.upstream:
            parts.append(
                f"Forwarding to {urlsplit(resolved.upstream).netloc} "
                f"(your {resolved.upstream_env})."
            )
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
    return resolve_for_launch(cfg.commentator, cli=cli, binary=binary)


def _make_commentator_resolver(
    resolved: ResolvedCli | None,
    name: str,
    *,
    plugin_narrates: bool = False,
    wiring_note: str = "",
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
            notice = _launch_notice(resolved, status, plugin_narrates=plugin_narrates)
        else:
            notice = f"{_not_recognized_note(name)}  •  {status}"
        if wiring_note:
            notice = f"{notice}  •  {wiring_note}"
        return commentator_cfg, notice

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
    actual_proxy_port = proxy_port or free_port(proxy_bind_host())

    resolved: ResolvedCli | None
    try:
        resolved = resolve_cli_for_vc(
            name, cfg_for_resolve, host=proxy_bind_host(), port=actual_proxy_port
        )
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        return

    companion_cli = resolved.profile_id if resolved is not None else name
    # A CLI whose plugin reports everything (Pi) is narrated through it; the
    # proxy would only duplicate those events, or miss them on another provider.
    plugin_narrates = integrations.plugin_narrates(companion_cli)
    if plugin_narrates and resolved is not None:
        resolved = dataclasses.replace(
            resolved, env_overrides={}, launch_args=(), upstream=None, upstream_env=None
        )
    enable_proxy = resolved is not None and not plugin_narrates
    # Plugin sessions register here too, so it exists even without the proxy.
    sessions = SessionRegistry()
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

    companion = _server_app_state(server).companion
    launch = companion.new_launch(companion_cli, proxied=enable_proxy)
    wiring = integrations.session_wiring(
        companion_cli, port=port, launch_id=launch.id, proxied=enable_proxy
    )
    resolve_commentator = _make_commentator_resolver(
        resolved, name, plugin_narrates=plugin_narrates, wiring_note=wiring.note
    )
    commentator_cfg, launch_notice = resolve_commentator(cfg)
    commentator = Commentator(bus, commentator_cfg, cfg.commentator_language, sessions=sessions)
    _server_app_state(server).commentator = commentator
    _server_app_state(server).commentator_resolver = resolve_commentator
    _server_app_state(server).launch_notice = launch_notice
    _server_app_state(server).focus_router = focus_router
    servers: list[uvicorn.Server] = [server]
    if enable_proxy:
        upstreams = (
            {resolved.provider: resolved.upstream}
            if resolved is not None and resolved.upstream
            else None
        )
        servers.append(
            build_proxy_server(
                bus,
                host=proxy_bind_host(),
                port=actual_proxy_port,
                registry=sessions,
                quiet=True,
                upstreams=upstreams,
            )
        )
    server_tasks = _start_servers(servers)
    extra: list[asyncio.Task[Any]] = [asyncio.create_task(commentator.run(), name="commentator")]
    tts_driver, tts_task = _start_tts_driver(bus, hub, cfg, server)
    extra.append(tts_task)
    focus_router.on_narrate_gate(tts_driver.set_focus_gate)

    focus_router.start()

    # Status goes to the browser panel (via /api/info), not the console — the
    # child CLI owns the terminal, so anything we print there is wiped when the
    # PTY clears the screen on handover.
    if resolved is not None:
        binary = resolved.resolved_binary
        # The plugin flag goes first: the user's own args may be a subcommand.
        launch_args = [*wiring.args, *resolved.launch_args]
        full_env = child_env({**resolved.env_overrides, **wiring.env})
        cwd = str(resolved.working_directory) if resolved.working_directory else None
        # Wait for the proxy to bind before the child starts using its base URL.
        # The child owns the terminal here, so a failure goes to the log file
        # (routed by `_route_logging_to_file`), never the console.
        if enable_proxy and not await wait_for_port(
            proxy_bind_host(), actual_proxy_port, timeout=10.0
        ):
            logging.getLogger(__name__).warning(
                "proxy did not come up on %s:%s in time — first request may be unnarrated",
                proxy_bind_host(),
                actual_proxy_port,
            )
    else:
        binary = name
        launch_args = list(wiring.args)
        full_env = child_env(wiring.env)
        cwd = None

    adapter = PtyAdapter(bus, [binary, *launch_args, *cli_args], env=full_env, cwd=cwd)
    dialog = DialogManager(bus, adapter, cfg.dialog)
    _server_app_state(server).dialog = dialog
    extra.append(asyncio.create_task(dialog.run(), name="dialog"))

    try:
        await adapter.start()
    except (RuntimeError, OSError) as e:  # OSError: `vc typo` — no such program
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
