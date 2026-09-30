"""System tray icon (pystray). Runs in its own daemon thread."""

from __future__ import annotations

import logging
import threading
import webbrowser
from collections.abc import Callable
from typing import Any

_UNAVAILABLE: str | None = None
try:
    import pystray
    from PIL import Image, ImageDraw
except Exception as _exc:  # pragma: no cover
    # ImportError when the extra is missing; on a headless Linux box pystray
    # picks its Xorg backend at import time and raises Xlib's
    # DisplayNameError instead. Either way: no tray, everything else runs.
    pystray = None
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    _UNAVAILABLE = f"{type(_exc).__name__}: {_exc}"

log = logging.getLogger(__name__)


def _icon_image() -> Any:
    im = Image.new("RGB", (64, 64), (20, 30, 60))
    d = ImageDraw.Draw(im)
    d.ellipse((14, 14, 50, 50), fill=(122, 162, 255))
    d.ellipse((26, 26, 38, 38), fill=(20, 30, 60))
    return im


class TrayService:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        on_quit: Callable[[], None] | None = None,
        url: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        #: The panel link, with the token when the panel needs one here.
        self._url = url
        #: Called from the tray's thread when the user picks Quit.
        self._on_quit = on_quit
        self._icon: Any = None
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return pystray is not None

    def start(self) -> None:
        if not self.available:
            log.info("tray icon unavailable (%s); skipping", _UNAVAILABLE)
            return
        url = self._url or f"http://{self._host}:{self._port}/"

        def on_open(icon: Any, item: Any) -> None:
            webbrowser.open(url)

        def on_quit(icon: Any, item: Any) -> None:
            icon.stop()
            # Removing the icon alone left the app running with no way back
            # to it; Quit means quit.
            if self._on_quit is not None:
                self._on_quit()

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
