"""Which model is the weakest and which the strongest a CLI can run right now.

The narrator wants the cheapest model that can write two sentences; the
supervisor wants the most capable one the account has. Hard-coding either rots
the moment a vendor renames a tier, so where a CLI keeps a live catalog we read
it, and only fall back to the static tiers in `cli_profiles` otherwise.

Codex is the one with a real catalog: it refreshes `models_cache.json` under
`$CODEX_HOME` on every start, with the account's own slugs, a `priority` (lower
is the more capable, preferred tier) and a `visibility` flag. Claude Code has
no such file — its CLI takes the `haiku` / `sonnet` / `opus` aliases — so its
tiers are static by necessity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from voice_copilot.commentator.cli_profiles import NARRATION_PROFILES

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelTiers:
    weakest: str
    strongest: str
    #: Where the pair came from — surfaces in the panel so a surprising pick
    #: can be traced ("catalog" = read from the CLI's own list).
    source: str
    #: Everything the catalog offered, strongest first; empty for static tiers.
    catalog: tuple[str, ...] = ()


#: Static tiers for CLIs without a readable catalog. `opus` rather than the
#: narration profile's `sonnet`: the auto-tier checkbox asks for the strongest.
_STATIC_TIERS: dict[str, tuple[str, str]] = {
    "claude": ("claude-haiku-4-5-20251001", "opus"),
}

#: Codex catalog entries that are not general-purpose models.
_CODEX_EXCLUDE = ("review", "reserve")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _codex_catalog(path: Path) -> list[tuple[int, str]]:
    """(priority, slug) for every listed, general-purpose model in the cache."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.debug("model catalog: cannot read %s: %s", path, e)
        return []
    models = doc.get("models") if isinstance(doc, dict) else None
    out: list[tuple[int, str]] = []
    for m in models or []:
        if not isinstance(m, dict):
            continue
        slug = m.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        if m.get("visibility") not in (None, "list"):
            continue
        if any(word in slug for word in _CODEX_EXCLUDE):
            continue
        priority = m.get("priority")
        out.append((priority if isinstance(priority, int) else 10_000, slug))
    return sorted(out)


def _narrator_pick(strongest_first: tuple[str, ...]) -> str:
    """The cheapest model that can still write two sentences of prose.

    The bottom of the catalog by priority is a code-specialised fast tier
    (`gpt-5.3-codex-spark`), which in testing echoed the event labels back
    instead of narrating. A "mini" general model is the better narrator when
    the account has one, so it wins; the last entry is the fallback.
    """
    for slug in reversed(strongest_first):
        if "mini" in slug:
            return slug
    return strongest_first[-1]


def model_tiers(cli: str | None) -> ModelTiers | None:
    """Weakest / strongest model for `cli`, or None if nothing is known."""
    if not cli:
        return None
    if cli == "codex":
        ranked = _codex_catalog(codex_home() / "models_cache.json")
        if len(ranked) >= 2:
            slugs = tuple(slug for _, slug in ranked)
            return ModelTiers(
                weakest=_narrator_pick(slugs), strongest=slugs[0], source="catalog", catalog=slugs
            )
    static = _STATIC_TIERS.get(cli)
    if static is not None:
        return ModelTiers(weakest=static[0], strongest=static[1], source="static")
    profile = NARRATION_PROFILES.get(cli)
    if profile is not None and profile.model and profile.strong_model:
        return ModelTiers(weakest=profile.model, strongest=profile.strong_model, source="static")
    return None


#: For `vc model`. A CLI with no way to list its models takes a name instead.
_LIST_COMMANDS = {
    "pi": ("--list-models",),
    "omp": ("models", "ls", "--json"),
    "opencode": ("models",),
}
_ALIASES = {"claude": ("haiku", "sonnet", "opus")}
_OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"
_LIST_TIMEOUT_S = 20.0


def _parse_listing(cli: str, output: str) -> list[str]:
    if cli == "omp":
        return _parse_omp_listing(output)
    rows = [line.split() for line in output.splitlines() if line.strip()]
    if cli == "opencode":
        return [row[0] for row in rows if "/" in row[0]]
    # A table: the provider, then the model, under a header line.
    return [f"{row[0]}/{row[1]}" for row in rows[1:] if len(row) >= 2 and "/" not in row[0]]


def _parse_omp_listing(output: str) -> list[str]:
    try:
        doc = json.loads(output)
    except ValueError:
        return []
    rows = doc.get("models", doc) if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        return []
    return [
        f"{row['provider']}/{row['id']}"
        for row in rows
        if isinstance(row, dict) and row.get("provider") and row.get("id")
    ]


def _run_listing(cli: str) -> list[str]:
    binary = shutil.which(cli)
    if binary is None:
        return []
    try:
        done = subprocess.run(
            [binary, *_LIST_COMMANDS[cli]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_LIST_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return _parse_listing(cli, done.stdout)


async def listed_models(cli: str | None) -> list[str]:
    """The models this CLI offers, as far as it will say, best known first."""
    if not cli:
        return []
    if cli in _ALIASES:
        return list(_ALIASES[cli])
    if cli in _LIST_COMMANDS:
        return await asyncio.to_thread(_run_listing, cli)
    tiers = model_tiers(cli)
    if tiers is None:
        return []
    return list(tiers.catalog) if tiers.catalog else [tiers.weakest, tiers.strongest]


async def ollama_models() -> list[str]:
    """What the local Ollama has, if one is running. It needs no key."""
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.get(_OLLAMA_TAGS)
        names = [str(m["name"]) for m in response.json().get("models", [])]
        return [name for name in names if "embed" not in name]  # These can't write a sentence.
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return []
