from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

CliKind = Literal["cli", "shell"]


@dataclass(frozen=True)
class CliCatalogEntry:
    label: str
    command: str
    description: str
    website_url: str
    provider: str
    base_url_env: str
    #: Extra argv the launcher inserts before the user's own args, for CLIs
    #: that ignore base-URL env vars and take the endpoint as a config flag
    #: instead (Codex: `-c openai_base_url="..."`). `{proxy_url}` is
    #: substituted with this profile's route on the running proxy.
    launch_args: tuple[str, ...] = ()
    #: "cli" launches a binary; "shell" opens a plain terminal with every
    #: proxy route exported, so anything started inside it is narrated.
    kind: CliKind = "cli"
    #: 2-char monogram + badge colour for the launcher grid (no vendor logos).
    icon: str = ""
    accent: str = ""
    #: Position in the YAML file — the curated order of the launcher list.
    order: int = 0


_CATALOG_PATH = Path(__file__).with_name("cli_catalog.yaml")


@lru_cache(maxsize=1)
def load_cli_catalog() -> dict[str, CliCatalogEntry]:
    raw = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RuntimeError("proxy CLI catalog must be a mapping")
    catalog: dict[str, CliCatalogEntry] = {}
    for order, (profile_id, payload) in enumerate(raw.items()):
        if not isinstance(profile_id, str) or not isinstance(payload, dict):
            continue
        kind = str(payload.get("kind") or "cli")
        catalog[profile_id] = CliCatalogEntry(
            label=str(payload.get("label") or profile_id),
            command=str(payload.get("command") or profile_id),
            description=str(payload.get("description") or ""),
            website_url=str(payload.get("website_url") or ""),
            provider=str(payload.get("provider") or "openai"),
            base_url_env=str(payload.get("base_url_env") or "OPENAI_BASE_URL"),
            launch_args=tuple(str(a) for a in (payload.get("launch_args") or ())),
            kind="shell" if kind == "shell" else "cli",
            icon=str(payload.get("icon") or profile_id[:2].upper()),
            accent=str(payload.get("accent") or "#7aa2ff"),
            order=order,
        )
    return catalog


CLI_CATALOG = load_cli_catalog()
