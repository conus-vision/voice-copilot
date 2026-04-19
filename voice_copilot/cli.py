"""Typer entrypoint.

Stage Э2 wires up `serve` to actually launch the FastAPI server so the popup
can be opened in a browser. `run` (wraps a target CLI) lands in Э5.
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer
import uvicorn
from rich.console import Console

from voice_copilot import __version__
from voice_copilot.adapters import ClaudeCodeAdapter, CodexAdapter
from voice_copilot.adapters.base import CLIAdapter
from voice_copilot.audio import AudioHub, TTSDriver
from voice_copilot.commentator import Commentator
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import Config, load_config
from voice_copilot.dialog import DialogManager
from voice_copilot.hotkeys import HotkeyService, default_bindings
from voice_copilot.providers import registry as provider_registry
# Side-effect imports register the providers in the registry.
from voice_copilot.providers import llm as _llm  # noqa: F401
from voice_copilot.providers import stt as _stt  # noqa: F401
from voice_copilot.providers import tts as _tts  # noqa: F401
from voice_copilot.proxy.server import base_urls_for, serve_proxy
from voice_copilot.tray import TrayService
from voice_copilot.web.demo import run_demo
from voice_copilot.web.server import create_app

app = typer.Typer(
    name="voice-copilot",
    help="Voice pair-programmer for LLM coding CLIs.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


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
) -> None:
    """Start the voice-copilot server (no target CLI attached)."""
    asyncio.run(_serve(
        host=host, port=port, open_browser=open_browser, demo=demo,
        enable_hotkeys=hotkeys, enable_tray=tray,
    ))


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
        False, "--proxy/--no-proxy",
        help="Route the child CLI's API traffic through our reverse-proxy "
             "so we can narrate `thinking` blocks.",
    ),
    proxy_port: int = typer.Option(8766, "--proxy-port"),
) -> None:
    """Wrap TARGET CLI, narrate its events, and expose the voice popup."""
    env = base_urls_for(host, proxy_port) if proxy else None

    if target == "claude":
        builder = lambda bus: ClaudeCodeAdapter(bus, binary=binary or "claude", env=env)  # noqa: E731
    elif target == "codex":
        builder = lambda bus: CodexAdapter(bus, binary=binary or "codex", env=env)  # noqa: E731
    else:
        console.print(f"[red]target {target!r} not supported yet.[/red] "
                      f"Supported: claude, codex. PTY fallback will come later.")
        raise typer.Exit(code=2)
    asyncio.run(_run_with_adapter(
        build_adapter=builder,
        prompt=prompt,
        host=host, port=port,
        open_browser=open_browser,
        enable_hotkeys=hotkeys, enable_tray=tray,
        enable_proxy=proxy, proxy_port=proxy_port,
    ))


@app.command()
def config() -> None:
    """Print the resolved config path. For editing, open the /settings page."""
    from voice_copilot.core.config import config_path

    console.print(str(config_path()))


def _start_tts_driver(bus: EventBus, hub: AudioHub, cfg: Config) -> asyncio.Task[None] | None:
    try:
        tts = provider_registry.build("tts", cfg.tts.name, dict(cfg.tts.options))
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]TTS provider unavailable: {e}[/yellow]")
        return None
    driver = TTSDriver(bus, hub, tts, cfg.language)
    return asyncio.create_task(driver.run(), name="tts.driver")


async def _boot(
    bus: EventBus,
    host: str,
    port: int,
    open_browser: bool,
    enable_hotkeys: bool,
    enable_tray: bool,
) -> tuple[uvicorn.Server, HotkeyService | None, TrayService | None, Config, AudioHub]:
    cfg = load_config()

    hub = AudioHub()
    stt_provider = None
    try:
        stt_provider = provider_registry.build("stt", cfg.stt.name, dict(cfg.stt.options))
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]STT provider unavailable: {e}[/yellow]")

    fast_app = create_app(bus, cfg, audio_hub=hub, stt_provider=stt_provider)
    uv_config = uvicorn.Config(fast_app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(uv_config)

    loop = asyncio.get_running_loop()
    hotkey_svc: HotkeyService | None = None
    tray_svc: TrayService | None = None

    if enable_hotkeys:
        try:
            hotkey_svc = HotkeyService(bus, loop, default_bindings(cfg.hotkeys))
            hotkey_svc.start()
        except Exception as e:  # noqa: BLE001
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
    server, hotkey_svc, tray_svc, cfg, hub = await _boot(bus, host, port, open_browser, enable_hotkeys, enable_tray)

    tasks: list[asyncio.Task[Any]] = [asyncio.create_task(server.serve(), name="web")]
    tts_task = _start_tts_driver(bus, hub, cfg)
    if tts_task is not None:
        tasks.append(tts_task)
    if demo:
        tasks.append(asyncio.create_task(run_demo(bus), name="demo"))
        commentator = Commentator(bus, cfg.commentator, cfg.language)
        tasks.append(asyncio.create_task(commentator.run(), name="commentator"))
    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        for t in tasks:
            t.cancel()
    finally:
        if hotkey_svc is not None:
            hotkey_svc.stop()
        if tray_svc is not None:
            tray_svc.stop()


async def _run_with_adapter(
    build_adapter,  # type: ignore[no-untyped-def]
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
    server, hotkey_svc, tray_svc, cfg, hub = await _boot(bus, host, port, open_browser, enable_hotkeys, enable_tray)

    commentator = Commentator(bus, cfg.commentator, cfg.language)
    tasks: list[asyncio.Task[Any]] = [
        asyncio.create_task(server.serve(), name="web"),
        asyncio.create_task(commentator.run(), name="commentator"),
    ]
    tts_task = _start_tts_driver(bus, hub, cfg)
    if tts_task is not None:
        tasks.append(tts_task)
    if enable_proxy:
        tasks.append(asyncio.create_task(
            serve_proxy(bus, host=host, port=proxy_port), name="proxy",
        ))
        console.print(
            f"[green]proxy → ANTHROPIC_BASE_URL=http://{host}:{proxy_port}/anthropic  "
            f"OPENAI_BASE_URL=http://{host}:{proxy_port}/openai/v1[/green]"
        )
        # Give uvicorn a moment to bind before the child CLI tries to use the URL.
        await asyncio.sleep(0.3)

    adapter: CLIAdapter = build_adapter(bus)
    dialog = DialogManager(bus, adapter, cfg.dialog)
    tasks.append(asyncio.create_task(dialog.run(), name="dialog"))
    try:
        await adapter.start(initial_prompt=prompt)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        for t in tasks:
            t.cancel()
        if hotkey_svc is not None:
            hotkey_svc.stop()
        if tray_svc is not None:
            tray_svc.stop()
        return

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        for t in tasks:
            t.cancel()
    finally:
        await adapter.stop()
        if hotkey_svc is not None:
            hotkey_svc.stop()
        if tray_svc is not None:
            tray_svc.stop()
