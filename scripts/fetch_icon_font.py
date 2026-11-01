#!/usr/bin/env python3
"""Download the Material Symbols subset the panel uses and store it in the package.

The panel used to pull its icon font from fonts.googleapis.com on every load.
That leaked each panel open to Google, broke the icons offline and behind
filtering proxies, and made the page wait on a third-party CDN. The panel now
ships the font itself: a subset holding only the icons it draws, with the
variable axes the stylesheet actually sets (fill, weight, grade, optical size).

Run this after you add an icon to ``index.html`` or ``app.js``:

    python scripts/fetch_icon_font.py

It collects every icon name the panel references, asks Google Fonts for a
woff2 subset with exactly those glyphs, and rewrites
``src/voice_copilot/web/static/fonts/``. ``tests/test_icon_font.py`` fails when
the panel references an icon the bundled subset does not contain.

The font is Material Symbols by Google, licensed under Apache 2.0; the license
text sits next to the font file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_STATIC = _REPO_ROOT / "src" / "voice_copilot" / "web" / "static"
_FONT_DIR = _STATIC / "fonts"
_FONT_FILE = _FONT_DIR / "material-symbols-rounded.woff2"
_MANIFEST = _FONT_DIR / "material-symbols-rounded.json"

FAMILY = "Material Symbols Rounded"
# The ranges cover every value style.css passes to font-variation-settings.
# A wider range costs bytes for glyph variations nobody renders.
AXES = "opsz,wght,FILL,GRAD@20..24,500..600,0..1,-25"
# Google serves woff2 only to browsers it recognises.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_SPAN_ICON = re.compile(r'class="material-symbols-rounded"[^>]*>\s*([a-z0-9_]+)\s*<')
_SET_ICON = re.compile(r"setIcon\([^;]*?\)")
_QUOTED = re.compile(r'"([a-z0-9_]+)"')


def icons_in_panel(static_dir: Path = _STATIC) -> list[str]:
    """Every icon name index.html and app.js put into a Material Symbols span."""
    names: set[str] = set()
    for path in (static_dir / "index.html", static_dir / "app.js"):
        text = path.read_text(encoding="utf-8")
        names.update(_SPAN_ICON.findall(text))
        for call in _SET_ICON.findall(text):
            names.update(_QUOTED.findall(call))
    # `setIcon(el, iconName)` is the helper's own definition, not a use.
    names.discard("iconName")
    return sorted(names)


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return bytes(response.read())


def fetch(icons: list[str]) -> tuple[bytes, str]:
    family = FAMILY.replace(" ", "+")
    css_url = (
        f"https://fonts.googleapis.com/css2?family={family}:{AXES}"
        f"&icon_names={','.join(icons)}&display=block"
    )
    css = _get(css_url).decode("utf-8")
    match = re.search(r"src:\s*url\((https://[^)]+)\)\s*format\('woff2'\)", css)
    if not match:
        raise SystemExit(f"no woff2 URL in the Google Fonts response:\n{css}")
    return _get(match.group(1)), css_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--extra",
        default="",
        help="comma-separated icon names to include even though no file references them yet",
    )
    args = parser.parse_args(argv)
    icons = sorted(set(icons_in_panel()) | {n for n in args.extra.split(",") if n})
    font, css_url = fetch(icons)
    _FONT_DIR.mkdir(parents=True, exist_ok=True)
    _FONT_FILE.write_bytes(font)
    manifest = {"family": FAMILY, "axes": AXES, "icons": icons, "source": css_url}
    _MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {_FONT_FILE.relative_to(_REPO_ROOT)} ({len(font)} bytes, {len(icons)} icons)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
