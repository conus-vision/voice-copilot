"""Every proxy route must be selectable in the panel.

The Launch tab renders one <select> per CLI profile from a JS list. A route
that exists on the server but not in that list leaves the select empty, the
autosave then drops the field, and *every* settings save fails validation —
which is exactly what happened when the ChatGPT route was added.
"""

import re
from pathlib import Path

from voice_copilot.proxy.server import _PROVIDERS

_APP_JS = Path(__file__).resolve().parents[2] / "src/voice_copilot/web/static/app.js"


def test_every_server_route_is_offered_by_the_panel() -> None:
    src = _APP_JS.read_text(encoding="utf-8")
    block = src[src.index("PROXY_ROUTE_OPTIONS = [") :]
    block = block[: block.index("];")]
    offered = set(re.findall(r'\["([^"]+)",', block))
    missing = set(_PROVIDERS) - offered
    assert not missing, f"routes without a panel option: {sorted(missing)}"
