from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CliCatalogEntry:
    label: str
    command: str
    description: str
    website_url: str
    provider: str
    base_url_env: str


_CATALOG_PATH = Path(__file__).with_name("cli_catalog.yaml")


@lru_cache(maxsize=1)
def load_cli_catalog() -> dict[str, CliCatalogEntry]:
    raw = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RuntimeError("proxy CLI catalog must be a mapping")
    catalog: dict[str, CliCatalogEntry] = {}
    for profile_id, payload in raw.items():
        if not isinstance(profile_id, str) or not isinstance(payload, dict):
            continue
        catalog[profile_id] = CliCatalogEntry(
            label=str(payload.get("label") or profile_id),
            command=str(payload.get("command") or profile_id),
            description=str(payload.get("description") or ""),
            website_url=str(payload.get("website_url") or ""),
            provider=str(payload.get("provider") or "openai"),
            base_url_env=str(payload.get("base_url_env") or "OPENAI_BASE_URL"),
        )
    return catalog


CLI_CATALOG = load_cli_catalog()
