"""Settings saved in the panel apply without a restart.

The voice, speech input, hotkeys and the narrator's debounce were read once
at start, so a change in Settings did nothing until the user restarted
`serve` or `vc`, with no sign that it had not applied.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from voice_copilot.audio.hub import AudioHub
from voice_copilot.audio.tts_driver import TTSDriver
from voice_copilot.commentator.pipeline import Commentator
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig, Config, HotkeysConfig, ProviderConfig
from voice_copilot.core.events import Event, EventKind
from voice_copilot.hotkeys import HotkeyService, default_bindings
from voice_copilot.providers import registry
from voice_copilot.providers.tts.base import TTSChunk, TTSProvider, UnavailableTTS
from voice_copilot.web import server as web_server


class _Voice(TTSProvider):
    output_format = "mp3"

    def __init__(self, tag: str = "voice", **_: Any) -> None:
        self.tag = tag
        self.spoken: list[str] = []

    async def synthesize(
        self, text: str, *, language: str, voice: str | None = None
    ) -> AsyncIterator[TTSChunk]:
        self.spoken.append(text)
        yield TTSChunk(format="mp3", data=b"x")


class _BrokenVoice(TTSProvider):
    def __init__(self, **_: Any) -> None:
        raise RuntimeError("needs a key")

    async def synthesize(  # pragma: no cover - never built
        self, text: str, *, language: str, voice: str | None = None
    ) -> AsyncIterator[TTSChunk]:
        yield TTSChunk(format="mp3", data=b"")


class _Ears:
    def __init__(self, **_: Any) -> None:
        pass


class _Driver:
    def __init__(self) -> None:
        self.providers: list[TTSProvider] = []
        self.languages: list[str] = []

    def set_provider(self, tts: TTSProvider) -> None:
        self.providers.append(tts)

    def set_language(self, language: str) -> None:
        self.languages.append(language)


class _Hotkeys:
    def __init__(self) -> None:
        self.applied: list[tuple[HotkeysConfig, bool]] = []

    def apply_config(self, cfg: HotkeysConfig, *, voice_input: bool) -> None:
        self.applied.append((cfg, voice_input))


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list[Config]]:
    saved: list[Config] = []
    monkeypatch.setattr(web_server, "save_config", saved.append)
    monkeypatch.setitem(registry._registry["tts"], "test-voice", _Voice)
    monkeypatch.setitem(registry._registry["tts"], "broken-voice", _BrokenVoice)
    monkeypatch.setitem(registry._registry["stt"], "test-ears", _Ears)
    app = web_server.create_app(EventBus(), Config())
    app.state.tts_driver = _Driver()
    app.state.hotkeys = _Hotkeys()
    return TestClient(app, base_url="http://127.0.0.1:8765"), saved


def _save(client: TestClient, cfg: Config) -> Any:
    return client.post("/api/config", json=cfg.model_dump(mode="json"))


def test_a_new_voice_speaks_the_next_line(panel: tuple[TestClient, list[Config]]) -> None:
    client, saved = panel
    cfg = Config(commentator_language="uk")
    cfg.tts = ProviderConfig(name="test-voice", options={"tag": "new"})
    assert _save(client, cfg).status_code == 200
    driver = client.app.state.tts_driver
    assert [p.tag for p in driver.providers] == ["new"]
    assert driver.languages == ["uk"]
    assert len(saved) == 1


def test_an_unchanged_voice_is_not_rebuilt(panel: tuple[TestClient, list[Config]]) -> None:
    client, _ = panel
    assert _save(client, Config()).status_code == 200
    assert client.app.state.tts_driver.providers == []


def test_a_voice_that_cannot_start_is_refused_and_not_saved(
    panel: tuple[TestClient, list[Config]],
) -> None:
    client, saved = panel
    cfg = Config()
    cfg.tts = ProviderConfig(name="broken-voice")
    res = _save(client, cfg)
    assert res.status_code == 400
    assert "needs a key" in res.json()["detail"]
    assert saved == []
    assert client.app.state.tts_driver.providers == []


def test_speech_input_follows_the_settings(panel: tuple[TestClient, list[Config]]) -> None:
    client, _ = panel
    cfg = Config()
    cfg.voice_input.enabled = True
    cfg.stt = ProviderConfig(name="test-ears")
    assert _save(client, cfg).status_code == 200
    assert isinstance(client.app.state.stt_provider, _Ears)
    cfg.voice_input.enabled = False
    assert _save(client, cfg).status_code == 200
    assert client.app.state.stt_provider is None


def test_hotkeys_are_rebound_when_they_change(panel: tuple[TestClient, list[Config]]) -> None:
    client, _ = panel
    assert _save(client, Config()).status_code == 200
    hotkeys = client.app.state.hotkeys
    assert hotkeys.applied == []
    cfg = Config()
    cfg.hotkeys.mute_toggle = "ctrl+alt+m"
    assert _save(client, cfg).status_code == 200
    assert [(c.mute_toggle, v) for c, v in hotkeys.applied] == [("ctrl+alt+m", False)]


def test_the_hotkey_service_takes_new_combos() -> None:
    service = HotkeyService(
        EventBus(), asyncio.new_event_loop(), default_bindings(HotkeysConfig(), voice_input=False)
    )
    assert "push_to_talk" not in service.combos
    cfg = HotkeysConfig(mute_toggle="ctrl+alt+m")
    service.apply_config(cfg, voice_input=True)
    assert service.combos["mute_toggle"] == "ctrl+alt+m"
    assert "push_to_talk" in service.combos


def _line(text: str) -> Event:
    return Event(
        kind=EventKind.COMMENTATOR_UTTERANCE,
        source="commentator",
        payload={"text": text, "streaming": False, "language": "en"},
    )


async def test_the_driver_speaks_with_the_voice_it_was_given_last() -> None:
    bus, old, new = EventBus(), _Voice("old"), _Voice("new")
    driver = TTSDriver(bus, AudioHub(), old, "en")
    run = asyncio.create_task(driver.run())
    await asyncio.sleep(0.05)
    await bus.publish(_line("first"))
    await asyncio.sleep(0.2)
    driver.set_provider(new)
    await bus.publish(_line("second"))
    await asyncio.sleep(0.2)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    assert (old.spoken, new.spoken) == (["first"], ["second"])


async def test_a_voice_that_could_not_start_says_why_on_each_line() -> None:
    bus = EventBus()
    driver = TTSDriver(bus, AudioHub(), UnavailableTTS("voice 'piper' could not start"), "en")
    errors: list[str] = []
    async with bus.subscribe() as q:
        run = asyncio.create_task(driver.run())
        await asyncio.sleep(0.05)
        await bus.publish(_line("hello"))
        try:
            async with asyncio.timeout(2):
                while not errors:
                    ev = await q.get()
                    if ev.kind is EventKind.ERROR:
                        errors.append(ev.payload["message"])
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
    assert errors == ["voice 'piper' could not start"]


class _QuietLLM:
    prompt_style = "api"

    def stream_chat(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            if False:
                yield ""

        return gen()


async def test_the_narrator_takes_a_new_debounce_at_the_next_batch() -> None:
    bus = EventBus()
    commentator = Commentator(bus, CommentatorConfig(debounce_ms=400), "en", llm=_QuietLLM())  # type: ignore[arg-type]
    seen: list[float] = []

    async def step(q: Any, loop: Any, debounce_s: float, max_batch_s: float) -> None:
        seen.append(debounce_s)
        if len(seen) == 1:
            commentator.update_config(CommentatorConfig(debounce_ms=1500))
        await asyncio.sleep(0)

    commentator._step = step  # type: ignore[method-assign]
    run = asyncio.create_task(commentator.run())
    while len(seen) < 2:
        await asyncio.sleep(0.01)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    assert seen[:2] == [0.4, 1.5]
