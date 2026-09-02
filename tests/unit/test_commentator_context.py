"""What the commentator is actually fed: grouped activity, a stable anchor."""

from voice_copilot.commentator.format import build_narration_user
from voice_copilot.commentator.pipeline import clean_user_query
from voice_copilot.core.events import Event, EventKind


def _ev(kind: EventKind, **payload: object) -> Event:
    return Event(kind=kind, source="anthropic.proxy", payload=payload)


def test_tool_runs_collapse_into_one_line_per_activity() -> None:
    events = [
        _ev(EventKind.TOOL_CALL_STARTED, tool="Grep", input={"pattern": "extract_user_query"}),
        _ev(EventKind.TOOL_CALL_STARTED, tool="Read", input={"file_path": "src/proxy/a.py"}),
        _ev(EventKind.TOOL_CALL_STARTED, tool="Read", input={"file_path": "src/proxy/b.py"}),
        _ev(EventKind.TOOL_CALL_STARTED, tool="Read", input={"file_path": "src/web/c.py"}),
        _ev(EventKind.TOOL_CALL_STARTED, tool="Read", input={"file_path": "src/web/d.py"}),
        _ev(EventKind.TOOL_CALL_STARTED, tool="Bash", input={"command": "pytest -q"}),
    ]
    prompt = build_narration_user(user_query="fix it", summary=None, events=events)

    assert "- searched: extract_user_query" in prompt
    # Three names, then a count — enough to speak about, not a manifest.
    assert "- read: proxy/a.py, proxy/b.py, web/c.py (+1 more)" in prompt
    assert "- ran: pytest -q" in prompt


def test_edit_payload_never_reaches_the_prompt() -> None:
    events = [
        _ev(
            EventKind.TOOL_CALL_STARTED,
            tool="Edit",
            input={"file_path": "src/app.py", "new_string": "X" * 5000},
        ),
        _ev(EventKind.FILE_EDITED, path="src/app.py"),
    ]
    prompt = build_narration_user(user_query="fix it", summary=None, events=events)

    assert "- edited: src/app.py" in prompt
    assert "XXXX" not in prompt
    assert len(prompt) < 500


def test_failures_keep_their_own_line() -> None:
    events = [
        _ev(EventKind.TOOL_CALL_STARTED, tool="Bash", input={"command": "pytest -q"}),
        _ev(EventKind.TOOL_CALL_FINISHED, tool="Bash", is_error=True, preview="2 failed"),
    ]
    prompt = build_narration_user(user_query="fix it", summary=None, events=events)
    assert "- tool Bash FAILED: 2 failed" in prompt


def test_injected_blocks_are_stripped_from_the_anchor() -> None:
    raw = (
        "<system-reminder>\nToday's date is 2026-09-01.\n</system-reminder>\n\n"
        "Изучи файлы и напиши готовность проекта"
    )
    assert clean_user_query(raw) == "Изучи файлы и напиши готовность проекта"


def test_requests_the_cli_makes_for_itself_are_not_anchors() -> None:
    # Title generation, the quota probe and the safety classifier ride the same
    # connection as the real conversation — anchoring on them threw away the
    # queued narration mid-turn.
    assert clean_user_query("quota") is None
    assert (
        clean_user_query(
            "<session>do the thing</session>\n\nWrite the title in the predominant "
            "language of the session."
        )
        is None
    )
    assert (
        clean_user_query(
            "<transcript>\n{}\n</transcript>\n\nRespond with <severity>N</severity> ONLY."
        )
        is None
    )
    assert clean_user_query("<system-reminder>only this</system-reminder>") is None


def test_a_real_question_survives() -> None:
    assert clean_user_query("почему в Trace идёт поток USER?") == "почему в Trace идёт поток USER?"


class _DummyLLM:
    """Stand-in so the Commentator can be built without provider credentials."""

    prompt_style = "api"

    def stream_chat(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        raise AssertionError("not called")


def test_quiet_tools_are_kept_as_context_but_do_not_trigger_narration() -> None:
    from voice_copilot.commentator.pipeline import Commentator
    from voice_copilot.core.bus import EventBus
    from voice_copilot.core.config import CommentatorConfig

    commentator = Commentator(EventBus(), CommentatorConfig(), "en", llm=_DummyLLM())  # type: ignore[arg-type]

    read = _ev(EventKind.TOOL_CALL_STARTED, tool="Read", input={"file_path": "a.py"})
    # Below min_importance=normal, so it must not wake the narrator — but it
    # has to stay in the buffer, or narration can never name the files.
    assert commentator._admit(read) == "context"
    assert commentator._buffer == [read]

    answer = _ev(EventKind.AGENT_TEXT, text="done")
    assert commentator._admit(answer) == "normal"
    assert commentator._buffer == [read, answer]

    edit = _ev(EventKind.FILE_EDITED, path="a.py")
    assert commentator._admit(edit) == "high"
