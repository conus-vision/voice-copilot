from voice_copilot.audio.hub import AudioHub
from voice_copilot.audio.tts_driver import TTSDriver
from voice_copilot.core.bus import EventBus


class _FakeTTS:
    output_format = "mp3"

    async def synthesize(self, text: str, *, language: str):
        return
        yield  # pragma: no cover - never reached, makes this an async generator


def _make_driver() -> TTSDriver:
    return TTSDriver(EventBus(), AudioHub(), _FakeTTS(), "en")


def test_muted_is_false_by_default() -> None:
    assert _make_driver().muted is False


async def test_focus_gate_closing_mutes() -> None:
    driver = _make_driver()
    driver.set_focus_gate(False)
    assert driver.muted is True
    driver.set_focus_gate(True)
    assert driver.muted is False


async def test_manual_mute_holds_even_if_focus_gate_reopens() -> None:
    driver = _make_driver()
    driver.set_muted(True)
    driver.set_focus_gate(False)
    driver.set_focus_gate(True)
    assert driver.muted is True  # manual mute alone still holds it muted
    driver.set_muted(False)
    assert driver.muted is False


def test_focus_gate_is_a_noop_when_value_is_unchanged() -> None:
    driver = _make_driver()
    driver.set_focus_gate(True)  # already True by default — must not raise
    assert driver.muted is False
