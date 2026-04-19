"""System prompts for the commentator, one markdown file per language.

Loader falls back to English if the requested language isn't present yet.
The prompts are product surface — edit freely, keep them short.
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent
_FALLBACK = "en"


def load(language: str) -> str:
    path = _DIR / f"{language}.md"
    if not path.exists():
        path = _DIR / f"{_FALLBACK}.md"
    return path.read_text(encoding="utf-8").strip()
