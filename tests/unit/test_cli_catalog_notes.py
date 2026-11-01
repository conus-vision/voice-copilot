"""Catalog caveats reach the launcher list.

Some CLIs send their model traffic to their vendor whatever base URL they
are given; the catalog says so per entry, and the panel shows it next to
the Launch button so nobody waits for narration that cannot come.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from voice_copilot.core.config import Config
from voice_copilot.proxy import cli_catalog, cli_shims


def test_every_note_is_one_short_line() -> None:
    raw = yaml.safe_load(
        Path(cli_catalog.__file__).with_name("cli_catalog.yaml").read_text(encoding="utf-8")
    )
    for profile_id, entry in raw.items():
        note = entry.get("proxy_note")
        if note:
            assert len(" ".join(str(note).split())) <= 220, profile_id


def test_the_launcher_list_carries_the_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import dataclasses

    monkeypatch.setattr(cli_shims, "proxy_shim_dir", lambda: tmp_path / "shims")
    meta = dataclasses.replace(
        cli_catalog.CLI_CATALOG["amp"], proxy_note="Talks to its own backend."
    )
    monkeypatch.setitem(cli_shims.CLI_CATALOG, "amp", meta)
    described = {p["id"]: p for p in cli_shims.describe_cli_shims(Config())["profiles"]}
    assert described["amp"]["proxy_note"] == "Talks to its own backend."
    assert described["claude"]["proxy_note"] == ""
