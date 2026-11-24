import pytest

# Importing voice_copilot.hotkeys first selects pynput's no-op backend on a
# headless Linux runner (no DISPLAY), so test modules that import pynput
# directly collect instead of failing with "failed to acquire X connection".
import voice_copilot.hotkeys  # noqa: F401
from voice_copilot.commentator import model_catalog


@pytest.fixture(autouse=True)
def _no_real_codex_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test never asks the real `codex` for its models.

    On a machine with Codex installed it would run a subprocess and read the
    account's catalog; on CI there is none. Either way the result would not be
    the test's. Tests of that path put a fake `_ask_codex` in place.
    """
    monkeypatch.setattr(model_catalog, "_ask_codex", lambda binary: None)
    monkeypatch.setattr(model_catalog, "_codex_answers", {})
