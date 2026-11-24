"""Auto-picked model tiers and the per-CLI supervisor override."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from voice_copilot.commentator import model_catalog
from voice_copilot.commentator.provider_select import (
    resolve_commentator_provider,
    resolve_supervisor,
    supervisor_status_text,
)
from voice_copilot.core.config import (
    CommentatorCliOverride,
    CommentatorConfig,
    SupervisorConfig,
)

#: The conftest swaps `_ask_codex` for a stub; the tests of the real one keep it here.
_REAL_ASK_CODEX = model_catalog._ask_codex


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-5.6-sol", "priority": 1, "visibility": "list"},
                    {"slug": "gpt-reserve", "priority": 3, "visibility": "hide"},
                    {"slug": "gpt-5.5", "priority": 7, "visibility": "list"},
                    {"slug": "gpt-5.4-mini", "priority": 23, "visibility": "list"},
                    {"slug": "gpt-5.3-codex-spark", "priority": 26, "visibility": "list"},
                    {"slug": "codex-auto-review", "priority": 43, "visibility": "hide"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_codex_tiers_come_from_its_own_catalog(codex_home: Path) -> None:
    tiers = model_catalog.model_tiers("codex")
    assert tiers is not None
    assert tiers.source == "cache"  # no Codex to ask in this test
    assert tiers.strongest == "gpt-5.6-sol"
    # the cheapest *general* model narrates; the code-tuned spark tier below
    # it echoes event labels instead of prose
    assert tiers.weakest == "gpt-5.4-mini"
    assert tiers.catalog[-1] == "gpt-5.3-codex-spark"
    # hidden and special-purpose entries never get picked
    assert "gpt-reserve" not in tiers.catalog
    assert "codex-auto-review" not in tiers.catalog


def _installed_codex(
    monkeypatch: pytest.MonkeyPatch, *, stdout: str, returncode: int = 0
) -> list[list[str]]:
    """An installed `codex` whose `debug models` prints `stdout`; returns the calls."""
    calls: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="boom")

    monkeypatch.setattr(model_catalog, "_ask_codex", _REAL_ASK_CODEX)
    monkeypatch.setattr(model_catalog.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(model_catalog.subprocess, "run", run)
    return calls


#: What Codex 0.155.1 listed on an account whose shared cache had been rewritten
#: by the 0.171.0 desktop app with `gpt-6.2-sol` on top, a model the CLI is refused.
_OLDER_CLI_CATALOG = json.dumps(
    {
        "models": [
            {"slug": "gpt-reserve", "priority": 1, "visibility": "hide"},
            {"slug": "gpt-6-astra", "priority": 2, "visibility": "list"},
            {"slug": "gpt-6-sol", "priority": 3, "visibility": "list"},
            {"slug": "gpt-5.6-luna", "priority": 7, "visibility": "list"},
        ]
    }
)


def test_codex_tiers_follow_the_installed_cli_not_the_shared_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(
        json.dumps(
            {
                "client_version": "0.171.0",
                "models": [
                    {"slug": "gpt-6.2-sol", "priority": 1, "visibility": "list"},
                    {"slug": "gpt-6-astra", "priority": 6, "visibility": "list"},
                ],
            }
        ),
        encoding="utf-8",
    )
    calls = _installed_codex(monkeypatch, stdout=_OLDER_CLI_CATALOG)

    tiers = model_catalog.model_tiers("codex")

    assert tiers is not None
    assert tiers.source == "codex"
    assert tiers.strongest == "gpt-6-astra"
    assert "gpt-6.2-sol" not in tiers.catalog
    assert calls == [["/bin/codex", "debug", "models"]]


def test_codex_is_asked_once_not_on_every_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The launch is resolved on the event loop, on attach and on every settings save.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    calls = _installed_codex(monkeypatch, stdout=_OLDER_CLI_CATALOG)
    for _ in range(3):
        tiers = model_catalog.model_tiers("codex")
        assert tiers is not None and tiers.strongest == "gpt-6-astra"
    assert len(calls) == 1


def test_codex_that_cannot_list_its_models_falls_back_to_the_cache_file(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _installed_codex(monkeypatch, stdout="", returncode=2)  # e.g. no `debug models`
    tiers = model_catalog.model_tiers("codex")
    assert tiers is not None
    assert tiers.source == "cache"
    assert tiers.strongest == "gpt-5.6-sol"
    # the failure is kept too: a broken `codex` doesn't cost a subprocess per resolve
    model_catalog.model_tiers("codex")
    assert len(calls) == 1


def test_codex_without_a_catalog_falls_back_to_static(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))  # no models_cache.json here
    tiers = model_catalog.model_tiers("codex")
    assert tiers is not None and tiers.source == "static"


def test_claude_tiers_are_static_aliases() -> None:
    tiers = model_catalog.model_tiers("claude")
    assert tiers is not None
    assert tiers.strongest == "opus"


def test_unknown_cli_has_no_tiers() -> None:
    assert model_catalog.model_tiers("nope") is None


def test_auto_tier_picks_weakest_for_narrator_and_strongest_for_supervisor(
    codex_home: Path,
) -> None:
    cmt = CommentatorConfig(auto_tier_models=True, supervisor=SupervisorConfig(mode="watch"))
    provider = resolve_commentator_provider(cmt, cli="codex", binary="/bin/codex")
    assert provider.options["model"] == "gpt-5.4-mini"
    sup = resolve_supervisor(cmt, cli="codex")
    assert sup.model == "gpt-5.6-sol"


def test_explicit_models_beat_the_auto_tier(codex_home: Path) -> None:
    cmt = CommentatorConfig(
        auto_tier_models=True,
        per_cli={"codex": CommentatorCliOverride(model="gpt-5.4-mini", supervisor_model="gpt-5.5")},
    )
    assert (
        resolve_commentator_provider(cmt, cli="codex", binary="/bin/codex").options["model"]
        == "gpt-5.4-mini"
    )
    assert resolve_supervisor(cmt, cli="codex").model == "gpt-5.5"


def test_per_cli_supervisor_override_without_touching_the_narrator() -> None:
    cmt = CommentatorConfig(
        mode="auto",
        supervisor=SupervisorConfig(mode="off"),
        per_cli={"claude": CommentatorCliOverride(supervisor_mode="plus")},
    )
    # narrator still resolves to the global auto mode …
    assert resolve_commentator_provider(cmt, cli="claude", binary="/bin/claude").name == "auto"
    # … while the supervisor is switched on for this CLI only
    assert resolve_supervisor(cmt, cli="claude").mode == "plus"
    assert resolve_supervisor(cmt, cli="codex").mode == "off"


def test_status_text_names_the_mode_and_model() -> None:
    assert supervisor_status_text(SupervisorConfig(mode="off")) is None
    assert supervisor_status_text(SupervisorConfig(mode="watch")) == "Supervisor on"
    assert supervisor_status_text(SupervisorConfig(mode="plus", model="gpt-5.5")) == (
        "Supervisor+ (gpt-5.5) on"
    )
