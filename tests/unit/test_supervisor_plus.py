"""Supervisor+: the four ways of working, the reaction to a STOP, the alarms and sounds,
and a narrator or supervisor that runs through another CLI on its own login.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import yaml

from voice_copilot.adapters.base import CLIAdapter, QuickAsideCapability
from voice_copilot.commentator.pipeline import Commentator, _Batch, _SessionContext
from voice_copilot.commentator.provider_select import (
    resolve_commentator_provider,
    resolve_for_launch,
    resolve_supervisor,
)
from voice_copilot.companion.commands import Command, parse
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import (
    WORK_MODES,
    CommentatorCliOverride,
    CommentatorConfig,
    Config,
    DialogConfig,
    SupervisorConfig,
    work_mode,
)
from voice_copilot.core.events import Event, EventKind
from voice_copilot.dialog.manager import DialogManager


class _LLM:
    prompt_style = "api"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def stream_chat(self, messages, *, system=None, max_tokens=None, temperature=None):  # type: ignore[no-untyped-def]
        self.calls += 1

        async def gen() -> AsyncIterator[str]:
            yield self.reply

        return gen()


def _commentator(
    reply: str, *, mode: str = "plus", react: str = "hold", narrator: bool = True
) -> tuple[Commentator, _LLM]:
    llm = _LLM(reply)
    cfg = CommentatorConfig(
        narrator=narrator,
        supervisor=SupervisorConfig(mode=mode, react=react, pause_s=12),  # type: ignore[arg-type]
    )
    c = Commentator(EventBus(), cfg, "en", llm=llm)  # type: ignore[arg-type]
    c._supervisor = lambda: llm  # type: ignore[method-assign, assignment, return-value]
    return c, llm


def _ev(kind: EventKind, source: str = "hooks.claude", **payload: Any) -> Event:
    return Event(kind=kind, source=source, payload={"session_id": "s", **payload})


async def _published(c: Commentator, work) -> list[Event]:  # type: ignore[no-untyped-def]
    async with c._bus.subscribe() as q:
        await work()
        await asyncio.sleep(0.01)  # side tasks (alarms, sounds) land on the bus
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out


def _then(call):  # type: ignore[no-untyped-def]
    """A plain call, for `_published`, which awaits its work."""

    async def work() -> None:
        call()

    return work


def _kinds(events: list[Event]) -> list[EventKind]:
    return [e.kind for e in events]


# --- config: modes, the old name, the narrator never silent ----------------------


def test_the_four_modes_round_trip() -> None:
    for name, (narrator, supervisor) in WORK_MODES.items():
        assert work_mode(narrator, supervisor) == name


def test_an_old_config_with_guard_is_supervisor_plus() -> None:
    raw = yaml.safe_load(
        "commentator:\n  supervisor:\n    mode: guard\n  per_cli:\n"
        "    codex:\n      supervisor_mode: guard\n"
    )
    cfg = Config.model_validate(raw)
    assert cfg.commentator.supervisor.mode == "plus"
    assert cfg.commentator.supervisor.react == "hold"  # what guard did: stop and wait
    assert cfg.commentator.per_cli["codex"].supervisor_mode == "plus"


def test_supervisor_plus_alone_turns_the_narrator_off_but_never_leaves_silence() -> None:
    alone = CommentatorConfig(narrator=False, supervisor=SupervisorConfig(mode="plus"))
    effective, _ = resolve_for_launch(alone, cli="claude", binary="/bin/claude")
    assert effective.narrator is False
    nobody = CommentatorConfig(narrator=False, supervisor=SupervisorConfig(mode="off"))
    effective, _ = resolve_for_launch(nobody, cli="claude", binary="/bin/claude")
    assert effective.narrator is True


def test_a_per_cli_row_can_pick_supervisor_plus_alone() -> None:
    cmt = CommentatorConfig(
        per_cli={"codex": CommentatorCliOverride(narrator="off", supervisor_mode="plus")}
    )
    effective, status = resolve_for_launch(cmt, cli="codex", binary="/bin/codex")
    assert (effective.narrator, effective.supervisor.mode) == (False, "plus")
    assert "Supervisor+" in status
    effective, _ = resolve_for_launch(cmt, cli="claude", binary="/bin/claude")
    assert (effective.narrator, effective.supervisor.mode) == (True, "off")


# --- another CLI, on its own login ------------------------------------------------


def test_codex_supervises_claude_and_the_model_follows_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import voice_copilot.commentator.provider_select as select

    monkeypatch.setattr(select, "model_tiers", lambda cli: None)
    cmt = CommentatorConfig(
        per_cli={"claude": CommentatorCliOverride(supervisor_cli="codex")},
        supervisor=SupervisorConfig(mode="watch"),
    )
    assert resolve_supervisor(cmt, cli="claude").cli == "codex"
    # Watching codex itself, "through codex" is just the default.
    cmt.supervisor.cli = "codex"
    assert resolve_supervisor(cmt, cli="codex").cli is None


def test_the_narrator_can_run_through_another_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import voice_copilot.commentator.provider_select as select

    monkeypatch.setattr(select.shutil, "which", lambda name: f"/bin/{name}")
    cmt = CommentatorConfig(narrator_cli="claude")
    provider = resolve_commentator_provider(cmt, cli="codex", binary="/bin/codex")
    assert provider.name == "auto"
    assert provider.options["cli"] == "claude"
    assert provider.options["binary"] == "/bin/claude"


def test_a_missing_cli_is_an_error_not_a_quiet_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    import voice_copilot.commentator.provider_select as select

    monkeypatch.setattr(select.shutil, "which", lambda name: None)
    cmt = CommentatorConfig(narrator_cli="gemini")
    provider = resolve_commentator_provider(cmt, cli="codex", binary="/bin/codex")
    assert provider.options["cli"] == "gemini"
    assert "binary" not in provider.options  # the provider says it isn't on PATH when called


async def test_the_supervisor_builds_its_llm_for_its_own_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import voice_copilot.commentator.pipeline as pipeline

    built: list[tuple[str, dict[str, Any]]] = []

    def build(kind: str, name: str, options: dict[str, Any]) -> _LLM:
        built.append((name, options))
        return _LLM("OK")

    monkeypatch.setattr(pipeline.registry, "build", build)
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: f"/bin/{name}")
    cfg = CommentatorConfig(supervisor=SupervisorConfig(mode="watch", cli="codex"))
    Commentator(EventBus(), cfg, "en", llm=_LLM("x"))._supervisor()  # type: ignore[arg-type]
    name, options = built[-1]
    assert name == "auto" and options["cli"] == "codex" and options["binary"] == "/bin/codex"


# --- the reaction to a STOP --------------------------------------------------------


async def test_stop_with_pause_pauses_for_a_while() -> None:
    c, _ = _commentator("STOP\nDeleting the tests.", react="pause")
    batch = _Batch(events=[_ev(EventKind.TURN_ENDED, final=True)], session_key="s")
    events = await _published(c, lambda: c._supervise(batch, "turn ended"))
    stop = next(e for e in events if e.kind is EventKind.SUPERVISOR_STOP)
    assert stop.payload["resume_after_s"] == 12
    spoken = next(e for e in events if e.kind is EventKind.COMMENTATOR_UTTERANCE)
    assert spoken.payload["text"].startswith("Pausing the agent for 12 seconds.")
    cue = next(e for e in events if e.kind is EventKind.CUE)
    assert cue.payload["cue"] == "critical"


async def test_stop_with_say_does_not_touch_the_agent() -> None:
    c, _ = _commentator("STOP\nDeleting the tests.", react="say")
    batch = _Batch(events=[_ev(EventKind.TURN_ENDED, final=True)], session_key="s")
    events = await _published(c, lambda: c._supervise(batch, "turn ended"))
    assert EventKind.SUPERVISOR_STOP not in _kinds(events)
    assert EventKind.COMMENTATOR_UTTERANCE in _kinds(events)


async def test_a_warning_has_its_sound_under_supervisor_plus_only() -> None:
    c, _ = _commentator("WARN\nTests were not run.", mode="plus", react="say")
    batch = _Batch(events=[_ev(EventKind.TURN_ENDED, final=True)], session_key="s")
    events = await _published(c, lambda: c._supervise(batch, "turn ended"))
    assert [e.payload["cue"] for e in events if e.kind is EventKind.CUE] == ["warn"]
    said = next(e for e in events if e.kind is EventKind.COMMENTATOR_UTTERANCE)
    assert said.payload["urgent"] is True

    # The Supervisor stays as it was: spoken, no sound, and quiet out of focus.
    c, _ = _commentator("WARN\nTests were not run.", mode="watch")
    events = await _published(c, lambda: c._supervise(batch, "turn ended"))
    assert EventKind.CUE not in _kinds(events)
    said = next(e for e in events if e.kind is EventKind.COMMENTATOR_UTTERANCE)
    assert said.payload["urgent"] is False


async def test_finished_work_chimes_after_an_ok_verdict_and_a_chat_does_not() -> None:
    c, _ = _commentator("OK", mode="plus")
    batch = _Batch(events=[_ev(EventKind.TURN_ENDED, final=True)], session_key="s")
    events = await _published(c, lambda: c._supervise(batch, "turn ended"))
    assert EventKind.CUE not in _kinds(events)  # no tool calls: an answer, not work
    c._contexts["s"].tool_calls = 3
    events = await _published(c, lambda: c._supervise(batch, "turn ended"))
    assert [e.payload["cue"] for e in events if e.kind is EventKind.CUE] == ["done"]


@pytest.mark.parametrize("mode", ["off", "watch"])
async def test_the_narrator_and_the_supervisor_make_no_sounds(mode: str) -> None:
    c, _ = _commentator("OK", mode=mode)
    ctx = c._contexts.setdefault("s", _SessionContext())
    ctx.tool_calls = 2
    batch = _Batch(events=[_ev(EventKind.TURN_ENDED, final=True)], session_key="s")
    events = await _published(c, lambda: c._supervise(batch, "turn ended"))
    assert EventKind.CUE not in _kinds(events)


async def test_a_refused_edit_leaves_the_supervisors_recent_edits() -> None:
    c, _ = _commentator("OK")
    write = _ev(
        EventKind.TOOL_CALL_STARTED,
        tool="Write",
        tool_use_id="w1",
        input={"file_path": ".env", "content": "KEY=1"},
    )
    c._buffer.append(write)  # still waiting for the narrator when the alarm comes
    alarm = _ev(
        EventKind.SUPERVISOR_DANGER,
        id="w1",
        rule="secrets",
        excerpt=".env",
        reaction="hold",
        wait_s=540,
        before_run=True,
    )
    await _published(c, lambda: c._on_danger(alarm))
    ctx = c._contexts["s"]
    assert len(ctx.edits) == 1
    history = list(ctx.history)
    # The call is on record before the alarm about it.
    assert history.index(next(h for h in history if "Supervisor+ caught" in h)) > 0
    decided = _ev(
        EventKind.SUPERVISOR_DANGER_DECIDED,
        id="w1",
        rule="secrets",
        excerpt=".env",
        allowed=False,
        by="user",
    )
    await c._on_danger(decided)
    assert ctx.edits == []


# --- Supervisor+ alone ---------------------------------------------------------------


async def test_supervisor_plus_alone_reviews_without_narrating() -> None:
    c, llm = _commentator("WARN\nNo tests were run.", narrator=False)
    q: asyncio.Queue[Event] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    feed = [
        _ev(EventKind.USER_MESSAGE, text="fix the parser", delivery="observed"),
        _ev(EventKind.TOOL_CALL_STARTED, tool="Edit", input={"file_path": "parser.py"}),
        _ev(EventKind.FILE_EDITED, path="parser.py"),
        _ev(EventKind.AGENT_TEXT, text="Done, the parser is fixed."),
        _ev(EventKind.TURN_ENDED, final=True),
    ]

    async def run() -> None:
        for ev in feed:
            q.put_nowait(ev)
            await c._step(q, loop, 0.01, 0.04)
        for _ in range(20):
            if not c._review_in_flight("s"):
                break
            await asyncio.sleep(0.01)

    events = await _published(c, run)
    assert llm.calls == 1  # the review, and no narration call
    spoken = [e for e in events if e.kind is EventKind.COMMENTATOR_UTTERANCE]
    assert [e.payload["role"] for e in spoken] == ["supervisor"]
    assert "Files edited: parser.py." in c._contexts["s"].facts  # type: ignore[operator]


async def test_what_the_narrator_skips_still_reaches_the_supervisor() -> None:
    c, _ = _commentator("OK", mode="watch")
    c._cfg.speak_tool_calls = False
    c._admit(_ev(EventKind.TOOL_CALL_STARTED, source="openai.proxy", tool="Bash", input={}))
    assert c._contexts["s"].tool_calls == 1


# --- dangerous commands, said aloud ----------------------------------------------------


async def test_a_held_command_is_said_with_the_alarm_whatever_the_session() -> None:
    c, _ = _commentator("OK")
    alarm = _ev(
        EventKind.SUPERVISOR_DANGER,
        session_id="background",
        id="t1",
        rule="force_push",
        excerpt="git push --force",
        reaction="hold",
        wait_s=540,
        before_run=True,
    )
    events = await _published(c, lambda: c._on_danger(alarm))
    assert _kinds(events) == [EventKind.CUE, EventKind.COMMENTATOR_UTTERANCE]
    assert events[0].payload["cue"] == "critical"
    said = events[1].payload
    assert said["role"] == "supervisor" and said["session_id"] == "background"
    assert "a force push" in said["text"] and "waits for your answer" in said["text"]
    assert "force push" in c._contexts["background"].history[-1]


async def test_the_users_answer_goes_on_the_supervisors_record() -> None:
    c, _ = _commentator("OK")
    decided = _ev(
        EventKind.SUPERVISOR_DANGER_DECIDED,
        id="t1",
        rule="discard",
        excerpt="git reset --hard",
        allowed=False,
        by="user",
    )
    await c._on_danger(decided)
    assert c._contexts["s"].history[-1] == (
        "- the user said: don't run it (git reset --hard); it did not run"
    )


async def test_through_the_proxy_a_dangerous_command_can_only_be_called_out() -> None:
    c, _ = _commentator("OK")
    seen = _ev(
        EventKind.TOOL_CALL_STARTED,
        source="anthropic.proxy",
        tool="Bash",
        tool_use_id="x1",
        input={"command": "git push --force"},
    )
    events = await _published(c, _then(lambda: c._admit(seen)))
    alarm = next(e for e in events if e.kind is EventKind.SUPERVISOR_DANGER)
    assert alarm.payload["reaction"] == "say" and alarm.payload["before_run"] is False


async def test_a_hooked_command_is_not_called_out_twice() -> None:
    c, _ = _commentator("OK")
    seen = _ev(EventKind.TOOL_CALL_STARTED, tool="Bash", input={"command": "git push --force"})
    events = await _published(c, _then(lambda: c._admit(seen)))
    assert EventKind.SUPERVISOR_DANGER not in _kinds(events)


# --- the dialog manager lets a timed pause go -------------------------------------------


class _Agent(CLIAdapter):
    name = "fake"
    quick_aside = QuickAsideCapability.QUEUE

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._paused = False

    async def start(self, initial_prompt: str | None = None) -> None: ...

    async def stop(self) -> None: ...

    async def send_user_message(self, text: str, *, urgent: bool = False) -> None: ...

    async def pause(self) -> bool:
        self.calls.append("pause")
        self._paused = True
        return True

    async def resume(self) -> bool:
        self.calls.append("resume")
        self._paused = False
        return True

    @property
    def is_paused(self) -> bool:
        return self._paused


async def test_a_timed_pause_lets_the_agent_go() -> None:
    bus, agent = EventBus(), _Agent()
    task = asyncio.create_task(DialogManager(bus, agent, DialogConfig()).run())
    await asyncio.sleep(0.02)
    await bus.publish(
        Event(kind=EventKind.SUPERVISOR_STOP, source="t", payload={"resume_after_s": 0.05})
    )
    await asyncio.sleep(0.02)
    assert agent.is_paused
    await asyncio.sleep(0.1)
    task.cancel()
    assert agent.calls == ["pause", "resume"]


async def test_the_user_takes_over_a_timed_pause() -> None:
    bus, agent = EventBus(), _Agent()
    task = asyncio.create_task(DialogManager(bus, agent, DialogConfig()).run())
    await asyncio.sleep(0.02)
    await bus.publish(
        Event(kind=EventKind.SUPERVISOR_STOP, source="t", payload={"resume_after_s": 0.05})
    )
    await asyncio.sleep(0.02)
    await bus.publish(Event(kind=EventKind.USER_PAUSE_TOGGLE, source="web", payload={}))
    await asyncio.sleep(0.02)
    await bus.publish(Event(kind=EventKind.USER_PAUSE_TOGGLE, source="web", payload={}))
    await asyncio.sleep(0.1)
    task.cancel()
    # Resumed by the user, paused by the user, and the old timer didn't undo that.
    assert agent.calls == ["pause", "resume", "pause"]


# --- the chat words -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("vc mode", Command("mode")),
        ("vc mode plus", Command("mode", ("plus",))),
        ("vc режим плюс-комментатор", Command("mode", ("plus-narrator",))),
        ("vc mode narrator", Command("mode", ("narrator",))),
        ("vc react pause", Command("react", ("pause",))),
        ("vc реакция голос", Command("react", ("say",))),
        ("vc deny", Command("deny")),
        ("vc отказ", Command("deny")),
        ("vc review cli codex", Command("review", ("cli", "codex"))),
        ("vc review cli same", Command("review", ("cli", "same"))),
        ("vc model cli claude", Command("model", ("cli", "claude"))),
        ("vc анализ через codex", Command("review", ("cli", "codex"))),
    ],
)
def test_new_words(text: str, command: Command) -> None:
    assert parse(text) == command


@pytest.mark.parametrize(
    "text", ["vc mode turbo", "vc react later", "vc deny it", "vc review cli", "vc mode plus now"]
)
def test_new_words_are_strict(text: str) -> None:
    assert parse(text) is None
