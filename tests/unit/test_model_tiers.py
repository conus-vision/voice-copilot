"""Auto-picked model tiers and the per-CLI supervisor override."""

from __future__ import annotations

import json
from pathlib import Path

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
    assert tiers.source == "catalog"
    assert tiers.strongest == "gpt-5.6-sol"
    # the cheapest *general* model narrates; the code-tuned spark tier below
    # it echoes event labels instead of prose
    assert tiers.weakest == "gpt-5.4-mini"
    assert tiers.catalog[-1] == "gpt-5.3-codex-spark"
    # hidden and special-purpose entries never get picked
    assert "gpt-reserve" not in tiers.catalog
    assert "codex-auto-review" not in tiers.catalog


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
        per_cli={"claude": CommentatorCliOverride(supervisor_mode="guard")},
    )
    # narrator still resolves to the global auto mode …
    assert resolve_commentator_provider(cmt, cli="claude", binary="/bin/claude").name == "auto"
    # … while the supervisor is switched on for this CLI only
    assert resolve_supervisor(cmt, cli="claude").mode == "guard"
    assert resolve_supervisor(cmt, cli="codex").mode == "off"


def test_status_text_names_the_mode_and_model() -> None:
    assert supervisor_status_text(SupervisorConfig(mode="off")) is None
    assert supervisor_status_text(SupervisorConfig(mode="watch")) == "Supervisor on"
    assert supervisor_status_text(SupervisorConfig(mode="guard", model="gpt-5.5")) == (
        "Supervisor+ (gpt-5.5) on"
    )
