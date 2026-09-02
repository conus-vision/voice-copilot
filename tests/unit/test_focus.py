import pytest

from voice_copilot.focus import FocusRouter, is_last_focused, record_focus


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "voice_copilot.focus.shared_focus_state_path", lambda: tmp_path / "focus-state.json"
    )
    return tmp_path


def test_is_last_focused_false_when_no_state_recorded(isolated_state) -> None:
    assert is_last_focused(pid=1234) is False


def test_record_then_is_last_focused_for_same_pid(isolated_state) -> None:
    record_focus(pid=1234)
    assert is_last_focused(pid=1234) is True
    assert is_last_focused(pid=5678) is False


def test_later_record_supersedes_earlier_one(isolated_state) -> None:
    record_focus(pid=1111)
    record_focus(pid=2222)
    assert is_last_focused(pid=1111) is False
    assert is_last_focused(pid=2222) is True


@pytest.mark.asyncio
async def test_tick_sets_current_focus_from_terminal(monkeypatch, isolated_state) -> None:
    monkeypatch.setattr("voice_copilot.focus.is_console_window_focused", lambda: True)
    router = FocusRouter(narrate_only_when_focused=True)
    await router._tick()
    assert router.current_focus is True


@pytest.mark.asyncio
async def test_tick_sets_current_focus_from_panel(monkeypatch, isolated_state) -> None:
    monkeypatch.setattr("voice_copilot.focus.is_console_window_focused", lambda: False)
    router = FocusRouter(narrate_only_when_focused=True)
    router.set_panel_focus(True)
    await router._tick()
    assert router.current_focus is True


def test_should_narrate_uses_current_focus_when_checked() -> None:
    router = FocusRouter(narrate_only_when_focused=True)
    assert router._should_narrate() is False
    router._current = True
    assert router._should_narrate() is True


def test_should_narrate_uses_sticky_state_when_unchecked(isolated_state) -> None:
    router = FocusRouter(narrate_only_when_focused=False)
    record_focus(pid=424_242)  # some other instance holds the claim
    assert router._should_narrate() is False
    record_focus()  # now this one does
    assert router._should_narrate() is True


def test_unchecked_with_no_claim_on_record_narrates(isolated_state) -> None:
    # Nobody has claimed focus yet (fresh install, headless run): staying
    # silent until some window gets focus would read as "TTS is broken".
    router = FocusRouter(narrate_only_when_focused=False)
    assert router._should_narrate() is True
    record_focus(pid=999_999)  # another instance claims it
    assert router._should_narrate() is False
