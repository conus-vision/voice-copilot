"""Every narration language gets its supervisor in that language.

Only English and Russian ship supervisor prompts; Spanish, French and
Ukrainian fall back to the English file, whose "Answer in English." used to
make the supervisor speak English in the middle of a non-English narration.
"""

from __future__ import annotations

import pytest

from voice_copilot.commentator import pipeline
from voice_copilot.commentator.prompts import load, load_summary, load_supervisor


@pytest.mark.parametrize(
    ("language", "name"), [("uk", "Ukrainian"), ("es", "Spanish"), ("fr", "French")]
)
@pytest.mark.parametrize("style", ["api", "cli"])
def test_fallback_supervisor_prompt_names_the_narration_language(
    language: str, name: str, style: str
) -> None:
    prompt = load_supervisor(language, style)
    assert f"Answer in {name}." in prompt
    assert "Answer in English." not in prompt


def test_english_and_own_language_prompts_are_untouched() -> None:
    assert "Answer in English." in load_supervisor("en")
    assert "Answer in English." not in load_supervisor("ru")
    assert "Answer in English." not in load("uk")
    # The summary prompt already follows the narration's language.
    assert "Answer in English." not in load_summary("es")


@pytest.mark.parametrize("language", ["en", "es", "fr", "uk", "ru"])
def test_every_language_has_its_own_spoken_supervisor_prefix(language: str) -> None:
    assert language in pipeline._SPOKEN_PREFIX
