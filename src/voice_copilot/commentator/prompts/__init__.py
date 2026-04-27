"""System prompts for the commentator, one markdown file per language.

Two styles are supported:
  "api" — classic role-based system prompt; used when the LLM receives
           system and user messages as separate turns (Anthropic, OpenAI API,
           Ollama, etc.).  Files: {lang}.md / {lang}.summary.md
  "cli" — task-imperative flat text; used when system + user are concatenated
           into a single prompt string (copilot-cli subprocess, etc.).
           Files: {lang}.cli.md / {lang}.cli.summary.md
           Falls back to the "api" file if the cli variant is missing.

Loader falls back to English if the requested language isn't present yet.
The prompts are product surface — edit freely, keep them short.
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent
_FALLBACK = "en"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _resolve(language: str, suffix: str, style: str) -> Path:
    """Return the best matching prompt file path."""
    if style == "cli":
        # Prefer lang.cli.<suffix>, fall back to lang.<suffix>, then en.<suffix>
        candidates = [
            _DIR / f"{language}.cli.{suffix}",
            _DIR / f"{language}.{suffix}",
            _DIR / f"{_FALLBACK}.cli.{suffix}",
            _DIR / f"{_FALLBACK}.{suffix}",
        ]
    else:
        candidates = [
            _DIR / f"{language}.{suffix}",
            _DIR / f"{_FALLBACK}.{suffix}",
        ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]  # will raise on read if missing


def load(language: str, style: str = "api") -> str:
    return _read(_resolve(language, "md", style))


def load_summary(language: str, style: str = "api") -> str:
    """System prompt for the summary-update LLM call."""
    return _read(_resolve(language, "summary.md", style))
