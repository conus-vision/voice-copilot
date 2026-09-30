"""Narrator prompts: English instructions, output in the narration language.

Every language gets the same English rules; only the reply-language line and
the native example lines differ. The user messages are English too, and end
by naming the reply language right where the model starts writing.
"""

from __future__ import annotations

import pytest

from voice_copilot.commentator import pipeline
from voice_copilot.commentator.format import (
    build_narration_user,
    build_summary_user,
    build_supervisor_user,
)
from voice_copilot.commentator.prompts import LANGUAGE_NAMES, load, load_summary, load_supervisor
from voice_copilot.core.events import Event, EventKind

_LANGUAGES = ["en", "es", "fr", "uk", "ru"]
_NATIVE_SAMPLE = {
    "en": "Two edits in the parser",
    "es": "Dos cambios en el parser",
    "fr": "Deux modifications dans le parser",
    "uk": "Дві правки в парсері",
    "ru": "Две правки в парсере",
}


@pytest.mark.parametrize("language", _LANGUAGES)
@pytest.mark.parametrize("style", ["api", "cli"])
def test_every_prompt_names_the_reply_language(language: str, style: str) -> None:
    name = LANGUAGE_NAMES[language]
    assert load(language, style).endswith(f"Reply in {name}.")
    assert f"in {name}." in load_summary(language, style)
    assert f"sentences in {name}" in load_supervisor(language, style)


@pytest.mark.parametrize("language", _LANGUAGES)
def test_narrator_carries_native_examples_for_its_language_only(language: str) -> None:
    prompt = load(language)
    assert _NATIVE_SAMPLE[language] in prompt
    for other, sample in _NATIVE_SAMPLE.items():
        if other != language:
            assert sample not in prompt


def test_instructions_are_the_same_english_rules_for_every_language() -> None:
    rules = "Describe the work, not the worker."
    assert all(rules in load(language) for language in _LANGUAGES)
    # No template placeholder survives rendering.
    for language in _LANGUAGES:
        for prompt in (load(language), load_summary(language), load_supervisor(language)):
            assert "{language" not in prompt


def test_a_language_without_a_guide_still_gets_rules_and_reply_line() -> None:
    prompt = load("de")
    assert "Describe the work, not the worker." in prompt
    assert prompt.endswith("Reply in de.")
    assert "\n\n\n" not in prompt


def test_cli_style_tells_a_coding_cli_not_to_use_tools() -> None:
    assert load("en", "cli").startswith("Answer with the requested text only.")
    assert not load("en", "api").startswith("Answer with the requested text only.")


def _read(path: str) -> Event:
    return Event(
        kind=EventKind.TOOL_CALL_STARTED,
        source="anthropic.proxy",
        payload={"tool": "Read", "input": {"file_path": path}},
    )


@pytest.mark.parametrize("style", ["api", "cli"])
def test_user_messages_are_english_and_end_on_the_reply_language(style: str) -> None:
    narration = build_narration_user(
        user_query="почини парсер",
        summary="Смотрели парсеры.",
        events=[_read("src/proxy/openai.py")],
        style=style,
        language="ru",
    )
    query = "почини парсер"
    memory = "Смотрели парсеры."
    assert f"USER REQUEST:\n{query}" in narration
    assert f"SO FAR:\n{memory}" in narration
    assert "NEW EVENTS (the agent's actions):" in narration
    assert narration.endswith("Reply in Russian, one or two sentences:")
    # No square-bracket labels: copilot-cli greps for them.
    assert "[" not in narration

    summary = build_summary_user(prev_summary=None, events=[], narration="x", language="uk")
    assert summary.endswith("New memory in Ukrainian, two or three sentences:")

    verdict = build_supervisor_user(
        user_query="fix it", summary=None, history=[], reason="turn ended", language="es"
    )
    assert "GOAL:\nfix it" in verdict
    assert verdict.endswith("one or two sentences in Spanish):")


@pytest.mark.parametrize("language", _LANGUAGES)
def test_every_language_has_its_own_spoken_supervisor_prefix(language: str) -> None:
    assert language in pipeline._SPOKEN_PREFIX
