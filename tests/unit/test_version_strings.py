"""Every place that shows the version shows the same one.

The publish workflow checks pyproject.toml and __init__.py against the tag;
the panel, the API and the Hermes plugin carry copies that a release could
forget, and the changelog has to name the version being released.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

from voice_copilot import __version__
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import Config
from voice_copilot.web.server import create_app

_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _ROOT / "src" / "voice_copilot"


def test_pyproject_and_package_agree() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__


def test_the_panel_shows_the_package_version() -> None:
    html = (_PACKAGE / "web" / "static" / "index.html").read_text(encoding="utf-8")
    shown = set(re.findall(r"\b(\d+\.\d+\.\d+)\s+alpha\b", html))
    assert shown == {__version__}


def test_the_api_and_the_hermes_plugin_report_it() -> None:
    assert create_app(EventBus(), Config()).version == __version__
    manifest = _PACKAGE / "companion" / "assets" / "hermes" / "voice_copilot" / "plugin.yaml"
    assert str(yaml.safe_load(manifest.read_text(encoding="utf-8"))["version"]) == __version__


def test_the_changelog_names_it() -> None:
    headings = re.findall(r"^## (.+)$", (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
    # Work after a release goes under "Unreleased" until the next version.
    top = headings[1] if headings[0] == "Unreleased" else headings[0]
    assert top.split()[0] == __version__
