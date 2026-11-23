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


class _Routed(_LLM):
    """One fake model for the supervisor, the advisor and the review, told apart by prompt."""

    def __init__(self, verdict: str, advice: str = "WATCH: x\nOK", review: str = "Done.") -> None:
        super().__init__(verdict)
        self.replies = {"supervisor": verdict, "advisor": advice, "debrief": review}
        self.by_role: dict[str, int] = {}

    def stream_chat(self, messages, *, system=None, max_tokens=None, temperature=None):  # type: ignore[no-untyped-def]
        role = (
            "advisor"
            if "WATCH" in (system or "")
            else "debrief"
            if "spoken review" in (system or "")
            else "supervisor"
        )
        self.by_role[role] = self.by_role.get(role, 0) + 1
        self.calls += 1
        reply = self.replies[role]

        async def gen() -> AsyncIterator[str]:
            yield reply

        return gen()


def _commentator(
    reply: str,
    *,
    mode: str = "plus",
    react: str = "hold",
    narrator: bool = True,
    llm: _LLM | None = None,
    **sup: Any,
) -> tuple[Commentator, _LLM]:
    llm = llm or _LLM(reply)
    cfg = CommentatorConfig(
        narrator=narrator,
        supervisor=SupervisorConfig(mode=mode, react=react, pause_s=12, **sup),  # type: ignore[arg-type]
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


async def test_finished_work_chimes_then_the_review_is_spoken() -> None:
    llm = _Routed("OK", review="The agent fixed the parser; tests passed after the edit.")
    c, _ = _commentator("", llm=llm)
    ctx = c._contexts.setdefault("s", _SessionContext())
    batch = _Batch(events=[_ev(EventKind.TURN_ENDED, final=True)], session_key="s")
    events = await _published(c, lambda: c._review(batch, "turn ended", ctx))
    assert EventKind.CUE not in _kinds(events)  # no tool calls: an answer, not work
    assert "debrief" not in llm.by_role
    ctx.tool_calls = 3
    events = await _published(c, lambda: c._review(batch, "turn ended", ctx))
    kinds = [e.kind for e in events if e.kind in (EventKind.CUE, EventKind.COMMENTATOR_UTTERANCE)]
    assert kinds == [EventKind.CUE, EventKind.COMMENTATOR_UTTERANCE]
    review = events[[e.kind for e in events].index(EventKind.COMMENTATOR_UTTERANCE)].payload
    assert review["kind"] == "debrief" and review["urgent"] is True
    assert review["text"].startswith("The agent fixed the parser")


async def test_without_the_review_the_ok_verdict_chimes_alone() -> None:
    c, _ = _commentator("OK", debrief=False)
    ctx = c._contexts.setdefault("s", _SessionContext())
    ctx.tool_calls = 2
    batch = _Batch(events=[_ev(EventKind.TURN_ENDED, final=True)], session_key="s")
    events = await _published(c, lambda: c._review(batch, "turn ended", ctx))
    assert [e.payload["cue"] for e in events if e.kind is EventKind.CUE] == ["done"]
    assert EventKind.COMMENTATOR_UTTERANCE not in _kinds(events)


async def test_a_warning_at_the_end_comes_with_the_review_but_no_chime() -> None:
    llm = _Routed("WARN\nNo test ran after the last edit.", review="Unchecked.")
    c, _ = _commentator("", llm=llm)
    ctx = c._contexts.setdefault("s", _SessionContext())
    ctx.tool_calls = 2
    batch = _Batch(events=[_ev(EventKind.TURN_ENDED, final=True)], session_key="s")
    events = await _published(c, lambda: c._review(batch, "turn ended", ctx))
    assert [e.payload["cue"] for e in events if e.kind is EventKind.CUE] == ["warn"]
    said = [e.payload["text"] for e in events if e.kind is EventKind.COMMENTATOR_UTTERANCE]
    assert said == ["Heads up: No test ran after the last edit.", "Unchecked."]
    assert ctx.case.told == ["WARN: No test ran after the last edit."]


def test_the_review_closes_the_turn_so_the_narrator_does_not() -> None:
    c, _ = _commentator("OK")
    ctx = c._contexts.setdefault("s", _SessionContext())
    end = _Batch(events=[_ev(EventKind.TURN_ENDED, final=True)], session_key="s")
    assert not c._review_speaks_for(end)  # nothing done: the narrator gives the answer
    ctx.tool_calls = 1
    assert c._review_speaks_for(end)
    middle = _Batch(events=[_ev(EventKind.AGENT_TEXT, text="reading")], session_key="s")
    assert not c._review_speaks_for(middle)
    c._cfg.supervisor.debrief = False
    assert not c._review_speaks_for(end)


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
    llm = _Routed("WARN\nNo tests were run.", review="The agent edited parser.py; nothing ran.")
    c, _ = _commentator("", narrator=False, llm=llm)
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
        for _ in range(50):
            if not c._supervisor_tasks:
                break
            await asyncio.sleep(0.01)

    events = await _published(c, run)
    # The advisor on the plan, the review at the end and the spoken review; no narration.
    assert llm.by_role == {"advisor": 1, "supervisor": 1, "debrief": 1}
    spoken = [e for e in events if e.kind is EventKind.COMMENTATOR_UTTERANCE]
    assert [e.payload["role"] for e in spoken] == ["supervisor", "supervisor"]
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
        ("vc send", Command("send")),
        ("vc отправь", Command("send")),
        ("vc advice auto", Command("advice", ("auto",))),
        ("vc советы выкл", Command("advice", ("off",))),
    ],
)
def test_new_words(text: str, command: Command) -> None:
    assert parse(text) == command


@pytest.mark.parametrize(
    "text",
    [
        "vc mode turbo",
        "vc react later",
        "vc deny it",
        "vc review cli",
        "vc mode plus now",
        "vc send it now",
        "vc advice maybe",
    ],
)
def test_new_words_are_strict(text: str) -> None:
    assert parse(text) is None


# --- the advisor --------------------------------------------------------------------------

_SQL_NOTE = (
    "WATCH: SQL built from user input\nWATCH: empty query\n"
    "NOTE: The search puts the query straight into SQL: an injection risk.\n"
    "SEND: Use a parameterised query for the search."
)


def _edit_batch(c: Commentator, path: str) -> _Batch:
    ctx = c._contexts.setdefault("s", _SessionContext())
    ctx.files_edited.add(path)
    return _Batch(events=[_ev(EventKind.FILE_EDITED, path=path)], session_key="s")


async def test_the_advisor_looks_at_the_plan_and_puts_a_note_in_the_panel() -> None:
    llm = _Routed("OK", advice=_SQL_NOTE)
    c, _ = _commentator("", llm=llm)
    ctx = c._contexts.setdefault("s", _SessionContext())
    batch = _edit_batch(c, "web/customers.py")
    events = await _published(c, lambda: c._advise(batch, "plan", ctx))
    note = next(e.payload for e in events if e.kind is EventKind.SUPERVISOR_NOTE)
    assert note["text"].startswith("The search puts the query")
    assert note["send"] == "Use a parameterised query for the search."
    assert [e.payload["cue"] for e in events if e.kind is EventKind.CUE] == ["advice"]
    assert EventKind.COMMENTATOR_UTTERANCE not in _kinds(events)  # not spoken mid-task
    assert ctx.case.watch == ["SQL built from user input", "empty query"]
    # The same worry in other words is not raised twice.
    events = await _published(c, lambda: c._advise(batch, "8 tool calls", ctx))
    assert EventKind.SUPERVISOR_NOTE not in _kinds(events)


async def test_the_advisor_runs_on_the_plan_then_on_a_sensitive_file() -> None:
    c, _ = _commentator("OK", llm=_Routed("OK"))
    ctx = c._contexts.setdefault("s", _SessionContext())
    names: list[str] = []
    c._advise = lambda batch, reason, ctx: _record(names, reason)  # type: ignore[method-assign]
    c._advise_if_due(_edit_batch(c, "src/report.py"), ctx)
    c._advise_if_due(_edit_batch(c, "src/report2.py"), ctx)  # nothing new to look at
    await asyncio.sleep(0.01)
    ctx.sensitive_due, ctx.advised_at = "app/auth.py", 0.0
    c._advise_if_due(_edit_batch(c, "app/auth.py"), ctx)
    await asyncio.sleep(0.01)
    assert names == [
        "plan: the agent started changing files",
        "an edit to a sensitive file: app/auth.py",
    ]


async def _record(names: list[str], reason: str) -> None:
    names.append(reason)


def test_sensitive_files_are_noticed_on_record() -> None:
    c, _ = _commentator("OK")
    c._record_events("s", [_ev(EventKind.FILE_EDITED, path="migrations/0008_add_flag.py")])
    assert c._contexts["s"].sensitive_due == "migrations/0008_add_flag.py"
    c._record_events("s", [_ev(EventKind.FILE_EDITED, path="README.md")])
    assert c._contexts["s"].sensitive_due == "migrations/0008_add_flag.py"


async def test_a_note_goes_to_the_agent_only_when_the_user_sends_it() -> None:
    c, _ = _commentator("OK", llm=_Routed("OK", advice=_SQL_NOTE))
    ctx = c._contexts.setdefault("s", _SessionContext())
    events = await _published(c, lambda: c._advise(_edit_batch(c, "a.py"), "plan", ctx))
    assert EventKind.USER_MESSAGE not in _kinds(events)
    note_id = next(e.payload["id"] for e in events if e.kind is EventKind.SUPERVISOR_NOTE)
    send = Event(
        kind=EventKind.USER_ADVICE, source="web", payload={"id": note_id, "action": "send"}
    )
    events = await _published(c, lambda: c._on_advice(send))
    message = next(e for e in events if e.kind is EventKind.USER_MESSAGE)
    assert message.source == "web.advice"
    assert message.payload["text"] == "Use a parameterised query for the search."
    decided = next(e.payload for e in events if e.kind is EventKind.SUPERVISOR_NOTE_DECIDED)
    assert decided["status"] == "sent"
    assert ctx.case.notes[0].status == "sent"


async def test_vc_send_takes_the_latest_open_note_and_dismiss_puts_one_away() -> None:
    c, _ = _commentator("OK", llm=_Routed("OK", advice=_SQL_NOTE))
    ctx = c._contexts.setdefault("s", _SessionContext())
    await c._advise(_edit_batch(c, "a.py"), "plan", ctx)
    away = Event(kind=EventKind.USER_ADVICE, source="web", payload={"action": "dismiss"})
    await c._on_advice(away)
    assert ctx.case.notes[0].status == "dismissed"
    nothing = Event(kind=EventKind.USER_ADVICE, source="commands", payload={"action": "send"})
    events = await _published(c, lambda: c._on_advice(nothing))
    assert EventKind.USER_MESSAGE not in _kinds(events)  # nothing open is left to send


async def test_advice_auto_sends_at_once() -> None:
    c, _ = _commentator("OK", llm=_Routed("OK", advice=_SQL_NOTE), advice="auto")
    ctx = c._contexts.setdefault("s", _SessionContext())
    events = await _published(c, lambda: c._advise(_edit_batch(c, "a.py"), "plan", ctx))
    assert [e.payload["text"] for e in events if e.kind is EventKind.USER_MESSAGE] == [
        "Use a parameterised query for the search."
    ]


def test_the_advisor_stays_off_when_switched_off_or_outside_supervisor_plus() -> None:
    for mode, advice in (("plus", "off"), ("watch", "on")):
        c, _ = _commentator("OK", mode=mode, advice=advice)
        ctx = c._contexts.setdefault("s", _SessionContext())
        c._advise_if_due(_edit_batch(c, "a.py"), ctx)
        assert not ctx.advised_plan
