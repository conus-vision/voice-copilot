"""System tray icon (pystray). Runs in its own daemon thread."""

from __future__ import annotations

import logging
import threading
import webbrowser
from typing import Any

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    pystray = None
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


def _icon_image() -> Any:
    im = Image.new("RGB", (64, 64), (20, 30, 60))
    d = ImageDraw.Draw(im)
    d.ellipse((14, 14, 50, 50), fill=(122, 162, 255))
    d.ellipse((26, 26, 38, 38), fill=(20, 30, 60))
    return im


class TrayService:
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._icon: Any = None
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return pystray is not None

    def start(self) -> None:
        if not self.available:
            log.info("pystray not installed; skipping tray icon")
            return
        url = f"http://{self._host}:{self._port}/"

        def on_open(icon: Any, item: Any) -> None:
            webbrowser.open(url)

        def on_quit(icon: Any, item: Any) -> None:
            icon.stop()

        menu = pystray.Menu(
            pystray.MenuItem("Open popup", on_open, default=True),
            pystray.MenuItem("Quit", on_quit),
        )
        self._icon = pystray.Icon("voice-copilot", _icon_image(), "voice-copilot", menu)
        self._thread = threading.Thread(
            target=self._icon.run, name="voice-copilot-tray", daemon=True
        )
        self._thread.start()
        log.info("tray icon started")

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()
            self._icon = None
