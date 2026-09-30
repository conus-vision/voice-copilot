"""System prompts for the commentator: English instructions, any output language.

Each prompt is one English template (``narrator.md``, ``summary.md``,
``supervisor.md``). The narration language only changes two things:

* ``languages/<code>.md`` adds a short note on register plus native example
  lines, so the voice sounds like a person speaking that language rather
  than a translation of an English line;
* the prompt ends by naming the language to reply in, and the user message
  (see ``format.py``) repeats it right before the model starts writing.

Instructions stay in English on purpose. The models that narrate (Haiku,
GPT mini tiers, Gemini Flash, local models) follow English instructions most
reliably, one template keeps every language on the same rules, and a
translated prompt drifts out of date the day someone edits the English one.

Two styles are supported:
  "api" — system and user messages go to the model as separate turns
          (Anthropic, OpenAI, Ollama).
  "cli" — the prompt goes to a coding CLI in one-shot mode (``claude -p``,
          ``codex exec``, ``copilot``). Those CLIs are agents with tools, so
          the prompt opens with a line telling them to answer from the
          message alone.

A language with no ``languages/<code>.md`` still works: it gets the rules
and the reply-language line, without native examples.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

_DIR = Path(__file__).parent

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "uk": "Ukrainian",
    "ru": "Russian",
}

#: Prepended in "cli" style. A one-shot coding CLI is still an agent: without
#: this it may go and open the files the events mention.
_CLI_PREAMBLE = (
    "Answer with the requested text only. Do not use tools, read files or run commands: "
    "everything you need is in this message."
)


def language_name(language: str) -> str:
    """English name of a narration language code; unknown codes pass through."""
    return LANGUAGE_NAMES.get(language, language)


@cache
def _read(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8").strip()


def _language_guide(language: str) -> str:
    path = _DIR / "languages" / f"{language}.md"
    return _read(f"languages/{language}.md") if path.is_file() else ""


def _render(template: str, language: str, style: str) -> str:
    text = _read(template)
    text = text.replace("{language_guide}", _language_guide(language))
    text = text.replace("{language}", language_name(language))
    # A language without a guide leaves an empty paragraph behind.
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if style == "cli":
        text = f"{_CLI_PREAMBLE}\n\n{text}"
    return text


def load(language: str, style: str = "api") -> str:
    """System prompt for a narration line."""
    return _render("narrator.md", language, style)


def load_summary(language: str, style: str = "api") -> str:
    """System prompt for the summary-update call."""
    return _render("summary.md", language, style)


def load_supervisor(language: str, style: str = "api") -> str:
    """System prompt for the supervisor's checkpoint review."""
    return _render("supervisor.md", language, style)
