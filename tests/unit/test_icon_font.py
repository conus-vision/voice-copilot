"""The panel's icon font ships with the package and covers every icon it draws."""

from __future__ import annotations

import importlib.util
import json
import mimetypes
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import Config
from voice_copilot.web.server import STATIC_DIR, create_app

_REPO = Path(__file__).resolve().parents[2]
_FONT = STATIC_DIR / "fonts" / "material-symbols-rounded.woff2"
_MANIFEST = STATIC_DIR / "fonts" / "material-symbols-rounded.json"


def _fetch_script():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "fetch_icon_font", _REPO / "scripts" / "fetch_icon_font.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_panel_loads_nothing_from_google_fonts() -> None:
    for name in ("index.html", "style.css", "app.js"):
        text = (STATIC_DIR / name).read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in text, name
        assert "fonts.gstatic.com" not in text, name


def test_bundled_font_is_woff2_with_a_license() -> None:
    assert _FONT.read_bytes()[:4] == b"wOF2"
    assert (STATIC_DIR / "fonts" / "LICENSE-material-symbols.txt").is_file()


def test_every_icon_the_panel_draws_is_in_the_subset() -> None:
    used = set(_fetch_script().icons_in_panel(STATIC_DIR))
    assert used, "the icon scan found nothing; the regexes in fetch_icon_font.py drifted"
    bundled = set(json.loads(_MANIFEST.read_text(encoding="utf-8"))["icons"])
    missing = sorted(used - bundled)
    assert not missing, (
        f"icons {missing} are not in the bundled font; run `python scripts/fetch_icon_font.py`"
    )


def test_font_is_served_as_woff2() -> None:
    client = TestClient(create_app(EventBus(), Config()), base_url="http://127.0.0.1:8765")
    response = client.get("/static/fonts/material-symbols-rounded.woff2")
    assert response.status_code == 200
    assert response.headers["content-type"] == "font/woff2"
    assert response.content == _FONT.read_bytes()


def test_panel_files_keep_their_types_whatever_the_system_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Python takes MIME types from the system. On Windows that is the
    # registry: it has no .woff2, and on some machines .js reads text/plain.
    system = mimetypes.MimeTypes(filenames=())
    system.add_type("text/plain", ".js")
    monkeypatch.setattr(mimetypes, "_db", system)
    client = TestClient(create_app(EventBus(), Config()), base_url="http://127.0.0.1:8765")
    font = client.get("/static/fonts/material-symbols-rounded.woff2")
    assert font.headers["content-type"] == "font/woff2"
    script = client.get("/static/app.js")
    assert script.headers["content-type"].startswith("text/javascript")
