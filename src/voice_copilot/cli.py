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
from pathlib import Path
from typing import Any, cast

import typer
import uvicorn
from dotenv import find_dotenv, load_dotenv
from rich.console import Console

from voice_copilot import __version__
from voice_copilot.adapters import ClaudeCodeAdapter, CodexAdapter, PtyAdapter
from voice_copilot.adapters.base import CLIAdapter
from voice_copilot.alias_install import ensure_vc_alias
from voice_copilot.audio import AudioHub, TTSDriver
from voice_copilot.commentator import Commentator
from voice_copilot.commentator.provider_select import (
    commentator_status_text,
    resolve_commentator_provider,
    resolve_supervisor,
    supervisor_status_text,
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
from voice_copilot.proxy.cli_shims import ResolvedCli, resolve_cli_for_vc
from voice_copilot.proxy.server import (
    base_urls_for,
    build_proxy_server,
    provider_has_narration,
)
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
    servers: list[uvicorn.Server] = [server]
    if enable_proxy:
        servers.append(
            build_proxy_server(
                bus, host=host, port=actual_proxy_port, registry=sessions, quiet=True
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
