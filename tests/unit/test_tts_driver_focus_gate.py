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


class _Recording(_FakeTTS):
    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def synthesize(self, text: str, *, language: str):  # type: ignore[no-untyped-def]
        self.spoken.append(text)
        return
        yield  # pragma: no cover


async def _spoken_out_of_focus(*, urgent: bool, manual_mute: bool = False) -> list[str]:
    import asyncio

    from voice_copilot.core.events import Event, EventKind

    bus, tts = EventBus(), _Recording()
    driver = TTSDriver(bus, AudioHub(), tts, "en")
    run = asyncio.create_task(driver.run())
    await asyncio.sleep(0.05)
    driver.set_focus_gate(False)
    if manual_mute:
        driver.set_muted(True)
    line = {"text": "Dangerous command: a force push.", "streaming": False, "role": "supervisor"}
    await bus.publish(
        Event(
            kind=EventKind.COMMENTATOR_UTTERANCE,
            source="commentator",
            payload={**line, "urgent": urgent},
        )
    )
    await asyncio.sleep(0.2)
    run.cancel()
    return tts.spoken


async def test_supervisor_plus_alarms_are_spoken_out_of_focus() -> None:
    assert await _spoken_out_of_focus(urgent=True) == ["Dangerous command: a force push."]


async def test_other_lines_wait_for_focus_and_mute_silences_everything() -> None:
    assert await _spoken_out_of_focus(urgent=False) == []
    assert await _spoken_out_of_focus(urgent=True, manual_mute=True) == []
