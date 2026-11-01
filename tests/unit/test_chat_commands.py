"""The `vc` words: what counts as one, what each does, and how the proxy answers them.

The first rule above all: a message that merely mentions them goes to the agent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voice_copilot.adapters.hooks import HookAdapter
from voice_copilot.commentator.pipeline import Commentator
from voice_copilot.companion import hook_forward
from voice_copilot.companion.center import CommandCenter
from voice_copilot.companion.commands import WORDS, Command, parse, say
from voice_copilot.companion.runtime import (
    NO_HOOKS_ENV,
    RUNTIME_DIR_ENV,
    is_switched_off,
    set_session_switched_off,
    set_switched_off,
)
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig, Config
from voice_copilot.core.events import Event, EventKind
from voice_copilot.proxy.chat_commands import (
    ChatCommands,
    ends_with_the_users_words,
    is_a_model_call,
    synthetic_reply,
)
from voice_copilot.proxy.server import create_proxy_app
from voice_copilot.proxy.session import SessionRegistry
from voice_copilot.web import server as web_server

# ------------------------------------------------------------------ what counts


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("vc", Command("help")),
        ("  VC  ", Command("help")),
        ("vc help", Command("help")),
        ("vc off", Command("off")),
        ("VC OFF\n", Command("off")),
        ("vc off all", Command("off", ("all",))),
        ("vc on all", Command("on", ("all",))),
        ("vc выкл все", Command("off", ("all",))),
        ("vc status", Command("status")),
        ("vc check", Command("check")),
        ("vc speed", Command("speed")),
        ("vc speed 1.5", Command("speed", ("1.5",))),
        ("vc скорость 1,5", Command("speed", ("1,5",))),
        ("vc speed 2x", Command("speed", ("2x",))),
        ("vc volume 70", Command("volume", ("70",))),
        ("vc volume 70%", Command("volume", ("70%",))),
        ("vc mute", Command("mute")),
        ("vc unmute", Command("unmute")),
        ("vc pause", Command("pause")),
        ("vc go", Command("go")),
        ("vc дальше", Command("go")),
        ("vc model", Command("model")),
        ("vc model 2", Command("model", ("2",))),
        ("vc model ollama/gemma4:12b", Command("model", ("ollama/gemma4:12b",))),
        ("vc review", Command("review")),
        ("vc review guard", Command("review", ("guard",))),
        ("vc review model", Command("review", ("model",))),
        ("vc review model 3", Command("review", ("model", "3"))),
        ("vc анализ стоп", Command("review", ("guard",))),
        ("vc detail high", Command("detail", ("high",))),
        ("vc access files", Command("access", ("files",))),
        ("vc доступ файлы", Command("access", ("files",))),
        ("vc settings", Command("settings")),
        ("vc log", Command("log")),
        ("vc cost", Command("cost")),
        ("vc ask why is it reading that file?", Command("ask", ("why is it reading that file?",))),
        ("vc спроси зачем он это читает", Command("ask", ("зачем он это читает",))),
    ],
)
def test_these_are_commands(text: str, command: Command) -> None:
    assert parse(text) == command


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hello",
        "what does vc off do?",
        "the vc off command",
        "please run vc off",
        "vc off please",
        "vc off the build",
        "vc status of this repo",
        "vc model is broken",
        "vc model is the problem here",
        "vc speed fast",
        "vc speed up the tests",
        "vc volume up",
        "vc volume 70 please",
        "vc review the code",
        "vc review watch out",
        "vc detail everything",
        "vc access the database",
        "vc files",
        "vc commit",
        "vc commit -m fix",
        "vc: off",
        "vc.off",
        "vcoff",
        "vc-off",
        "vcs status",
        "vc ask",
        "vc off\nand then fix the tests",
        "vc status\n\nplease tell me what you think",
        "`vc off`",
        '"vc off"',
        "vc " + "x" * 3000,
    ],
)
def test_these_are_for_the_agent(text: str) -> None:
    assert parse(text) is None


def test_every_word_a_shell_takes_is_a_command() -> None:
    assert {"off", "on", "status", "speed", "model", "выкл"} <= WORDS


def test_the_list_of_commands_names_them_all() -> None:
    menu = say("menu", "en")
    for word in ("vc on", "vc off", "all", "vc check", "vc ask", "vc log", "vc cost", "vc speed"):
        assert word in menu
    for word in ("vc volume", "vc mute", "vc model", "vc review", "vc detail", "vc access"):
        assert word in menu
    assert "vc pause" in menu
    assert "vc go" in menu
    assert "vc settings" in menu


# ------------------------------------------------------------ one session or all


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RUNTIME_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(NO_HOOKS_ENV, raising=False)


def test_vc_off_stops_this_session_and_no_other() -> None:
    assert "this session" in hook_forward.run_command(Command("off"), session_id="a")["text"]
    assert is_switched_off("a")
    assert not is_switched_off("b")
    assert not is_switched_off()


def test_vc_off_all_stops_every_session_and_every_new_one() -> None:
    assert (
        "every session"
        in hook_forward.run_command(Command("off", ("all",)), session_id="a")["text"]
    )
    assert is_switched_off("a")
    assert is_switched_off("b")
    assert is_switched_off()


def test_vc_on_in_a_session_overrides_off_all_only_for_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hook_forward, "_ensure_companion", lambda: None)
    set_switched_off(True)
    hook_forward.run_command(Command("on"), session_id="a")
    assert not is_switched_off("a")
    assert is_switched_off("b")


def test_vc_on_all_clears_every_sessions_word(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook_forward, "_ensure_companion", lambda: None)
    set_session_switched_off("a", True)
    hook_forward.run_command(Command("on", ("all",)), session_id="b")
    assert not is_switched_off("a")
    assert not is_switched_off()


def test_vc_off_in_a_terminal_has_no_session_to_mean_so_it_means_all() -> None:
    hook_forward.run_command(Command("off"))
    assert is_switched_off()


def test_hooks_of_a_session_switched_off_go_quiet_while_others_report(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    posted: list[Any] = []
    monkeypatch.setattr(hook_forward, "_forward", lambda *a: posted.append(a) or b"")
    set_session_switched_off("a", True)

    class Stdin:
        def __init__(self, sid: str) -> None:
            body = {"hook_event_name": "PostToolUse", "session_id": sid}
            import io

            self.buffer = io.BytesIO(json.dumps(body).encode())

    for sid in ("a", "b"):
        monkeypatch.setattr("sys.stdin", Stdin(sid))
        assert hook_forward.main(["codex"]) == 0
    assert len(posted) == 1


# ------------------------------------------------------- `voice-copilot-hook --chat`


def test_chat_says_when_the_text_is_for_the_agent(capsys: Any) -> None:
    code = hook_forward.main(["--chat", "pi", "--session", "s", "--", "vc off please"])
    assert code == hook_forward._NOT_A_COMMAND_EXIT
    assert capsys.readouterr().out == ""


def test_chat_answers_a_command(capsys: Any) -> None:
    assert hook_forward.main(["--chat", "pi", "--session", "s", "--", "vc"]) == 0
    assert "vc off" in json.loads(capsys.readouterr().out)["text"]


def test_chat_says_nothing_inside_a_cli_voice_copilot_runs_itself(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv(NO_HOOKS_ENV, "1")
    assert hook_forward.main(["--chat", "pi", "--", "vc"]) == 0
    assert capsys.readouterr().out == ""


# ------------------------------------------------------------------ the greeting


async def test_a_new_session_is_told_how_to_get_the_list() -> None:
    adapter = HookAdapter(EventBus(), SessionRegistry(), greeting=lambda: "Connected. Type vc")
    reply = await adapter.handle_hook(
        "codex", {"hook_event_name": "SessionStart", "session_id": "s"}
    )
    assert reply == {"systemMessage": "Connected. Type vc"}


async def test_a_notice_goes_with_the_next_hook_where_nothing_reads_mail() -> None:
    adapter = HookAdapter(EventBus(), SessionRegistry())
    await adapter.handle_hook("codex", {"hook_event_name": "SessionStart", "session_id": "s"})
    adapter.notify("s", "The tests still fail.")
    reply = await adapter.handle_hook(
        "codex", {"hook_event_name": "PostToolUse", "session_id": "s"}
    )
    assert reply == {"systemMessage": "The tests still fail."}
    assert (
        await adapter.handle_hook("codex", {"hook_event_name": "PostToolUse", "session_id": "s"})
        is None
    )


async def test_a_notice_goes_with_the_mail_where_a_plugin_reads_it() -> None:
    adapter = HookAdapter(EventBus(), SessionRegistry())
    await adapter.handle_hook("pi", {"hook_event_name": "SessionStart", "session_id": "s"})
    await adapter.take_mail("pi", "s")
    adapter.notify("s", "The tests still fail.")
    assert await adapter.handle_hook("pi", {"hook_event_name": "Stop", "session_id": "s"}) is None
    assert adapter.take_notices("s") == ["The tests still fail."]


# ---------------------------------------------------------------- the commands


class _FakeLLM:
    prompt_style = "api"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def stream_chat(self, messages: Any, **_kwargs: Any) -> Any:
        self.prompts.append(messages[0].content)

        async def gen() -> Any:
            yield "It is waiting for the tests."

        return gen()


class _Hub:
    def __init__(self, clients: bool = True) -> None:
        self.clients = clients

    def has_clients(self) -> bool:
        return self.clients


def _center(monkeypatch: pytest.MonkeyPatch, *, hub: _Hub | None = None) -> dict[str, Any]:
    bus = EventBus()
    registry = SessionRegistry()
    llm = _FakeLLM()
    adapter = HookAdapter(bus, registry)
    commentator = Commentator(bus, CommentatorConfig(), "en", llm=llm)  # type: ignore[arg-type]
    monkeypatch.setattr(commentator, "_supervisor", lambda: llm)
    saved: list[Config] = []
    monkeypatch.setattr(web_server, "save_config", saved.append)
    app = FastAPI()
    app.state.commentator = commentator
    app.state.commentator_resolver = None
    app.state.config = Config()
    app.state.app = app
    center = CommandCenter(
        bus=bus,
        state=app.state,
        commentator=commentator,
        adapter=adapter,
        hub=hub or _Hub(),  # type: ignore[arg-type]
        panel_url="http://127.0.0.1:1/",
        active_cli=lambda: "claude",
        active_session=registry.get_active_id,
    )
    return {
        "bus": bus,
        "center": center,
        "app": app,
        "adapter": adapter,
        "saved": saved,
        "llm": llm,
        "commentator": commentator,
    }


async def _published(bus: EventBus, run: Any) -> tuple[Any, list[Event]]:
    async with bus.subscribe() as q:
        answer = await run
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return answer, events


async def test_speed_and_volume_go_to_the_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _center(monkeypatch)
    answer, events = await _published(w["bus"], w["center"].run("speed", ["1,5"]))
    assert "1.5" in answer["text"]
    assert [e.payload for e in events if e.kind is EventKind.PANEL_COMMAND] == [{"speed": 1.5}]
    _, events = await _published(w["bus"], w["center"].run("volume", ["70%"]))
    assert [e.payload for e in events if e.kind is EventKind.PANEL_COMMAND] == [{"volume": 70}]
    _, events = await _published(w["bus"], w["center"].run("mute", []))
    assert [e.payload for e in events if e.kind is EventKind.PANEL_COMMAND] == [{"muted": True}]
    assert w["app"].state.panel_overrides == {"speed": 1.5, "volume": 70, "muted": True}


async def test_nonsense_values_change_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _center(monkeypatch)
    answer, events = await _published(w["bus"], w["center"].run("speed", ["9"]))
    assert "from" in answer["text"]
    assert not [e for e in events if e.kind is EventKind.PANEL_COMMAND]
    answer, events = await _published(w["bus"], w["center"].run("volume", ["300"]))
    assert not [e for e in events if e.kind is EventKind.PANEL_COMMAND]


async def test_a_closed_panel_is_mentioned(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _center(monkeypatch, hub=_Hub(clients=False))
    assert "isn't open" in (await w["center"].run("volume", ["50"]))["text"]


async def test_review_detail_and_access_take_effect_and_are_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _center(monkeypatch)
    await w["center"].run("review", ["guard"])
    assert w["commentator"].supervisor_mode == "guard"
    await w["center"].run("detail", ["high"])
    assert w["commentator"]._cfg.min_importance == "low"
    answer = await w["center"].run("access", ["files"])
    assert "whole" in answer["text"]
    assert w["commentator"]._cfg.file_access == "files"
    assert w["saved"][-1].commentator.file_access == "files"


async def test_the_model_list_is_numbered_and_a_number_picks_from_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import voice_copilot.companion.center as center_module

    async def local() -> list[str]:
        return ["gemma4:12b"]

    monkeypatch.setattr(center_module, "ollama_models", local)
    w = _center(monkeypatch)
    answer = await w["center"].run("model", [])
    assert answer["choices"] == ["haiku", "sonnet", "opus", "ollama/gemma4:12b"]
    assert "2. sonnet" in answer["text"]

    await w["center"].run("model", ["2"])
    assert w["saved"][-1].commentator.per_cli["claude"].model == "sonnet"

    await w["center"].run("model", ["4"])
    saved = w["saved"][-1].commentator
    assert saved.per_cli["claude"].mode == "api"
    assert saved.provider.name == "openai-compat"
    assert saved.provider.options["model"] == "gemma4:12b"

    assert "no number" in (await w["center"].run("model", ["9"]))["text"]


async def test_a_model_that_needs_a_key_opens_the_settings_rather_than_asking_in_the_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import voice_copilot.companion.center as center_module

    opened: list[str] = []
    monkeypatch.setattr(center_module.webbrowser, "open", opened.append)
    monkeypatch.setattr(center_module, "get_secret", lambda _name: None)
    w = _center(monkeypatch)
    answer = await w["center"].run("model", ["openai/gpt-x"])
    assert "never in the chat" in answer["text"]
    assert opened == ["http://127.0.0.1:1/settings"]
    assert w["saved"] == []


async def test_a_question_is_answered_aloud_and_in_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _center(monkeypatch)
    await w["adapter"].handle_hook("pi", {"hook_event_name": "SessionStart", "session_id": "s"})
    async with w["bus"].subscribe() as q:
        answer = await w["center"].run("ask", ["why so slow?"], "s")
        assert "Asking" in answer["text"]
        async with asyncio.timeout(2):
            while True:
                event = await q.get()
                if event.kind is EventKind.COMMENTATOR_UTTERANCE:
                    break
    await asyncio.gather(*w["center"]._answers)
    assert event.payload["text"] == "It is waiting for the tests."
    assert "why so slow?" in w["llm"].prompts[0]
    assert w["adapter"].take_notices("s") == ["It is waiting for the tests."]


async def test_a_question_sees_what_the_narrator_hasnt_got_to_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _center(monkeypatch)
    edit = Event(
        kind=EventKind.TOOL_CALL_STARTED,
        source="hooks.pi",
        payload={
            "session_id": "s",
            "tool": "Edit",
            "input": {"file_path": "calc.py", "old_string": "a", "new_string": "subtract"},
        },
    )
    w["commentator"]._buffer.append(edit)
    await w["commentator"].answer_question("what changed?", "s")
    assert "calc.py" in w["llm"].prompts[0]
    assert "subtract" in w["llm"].prompts[0]

    # And when the narrator does get there, it isn't counted twice.
    ctx = w["commentator"]._contexts["s"]
    assert ctx.tool_calls == 1
    w["commentator"]._record_events("s", [edit])
    assert ctx.tool_calls == 1


@pytest.mark.parametrize(
    "tool_input",
    [
        {"file_path": "a.py", "old_string": "old", "new_string": "new"},
        {"filePath": "a.py", "oldString": "old", "newString": "new"},
        {"path": "a.py", "edits": [{"oldText": "old", "newText": "new"}]},
    ],
)
def test_the_supervisor_can_read_an_edit_from_any_cli(tool_input: dict[str, Any]) -> None:
    from voice_copilot.commentator.format import edit_excerpt

    excerpt = edit_excerpt({"tool": "edit", "input": tool_input})
    assert excerpt is not None
    assert "old" in excerpt
    assert "new" in excerpt


async def test_log_and_cost_report_what_was_said(monkeypatch: pytest.MonkeyPatch) -> None:
    w = _center(monkeypatch)
    assert "Nothing" in (await w["center"].run("log", []))["text"]
    await w["commentator"].answer_question("why?", None)
    assert "waiting" in (await w["center"].run("log", []))["text"]
    assert "1 questions" in (await w["center"].run("cost", []))["text"]


async def test_a_warning_goes_to_the_cli_when_nobody_is_there_to_hear_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _center(monkeypatch, hub=_Hub(clients=False))
    await w["adapter"].handle_hook("pi", {"hook_event_name": "SessionStart", "session_id": "s"})
    watcher = asyncio.create_task(w["center"].watch())
    await asyncio.sleep(0.01)
    await w["bus"].publish(
        Event(
            kind=EventKind.SUPERVISOR_VERDICT,
            source="t",
            payload={"status": "warn", "message": "The tests fail.", "session_id": "s"},
        )
    )
    await asyncio.sleep(0.01)
    watcher.cancel()
    assert any("The tests fail." in n for n in w["adapter"].take_notices("s"))


def test_files_are_read_only_inside_the_session_and_never_secrets(tmp_path: Path) -> None:
    from voice_copilot.commentator.format import read_edited_files

    (tmp_path / "app.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=1", encoding="utf-8")
    (tmp_path / "server.key").write_text("x", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("no", encoding="utf-8")
    texts = read_edited_files(["app.py", ".env", "server.key", "../outside.txt"], str(tmp_path))
    assert len(texts) == 1
    assert "print('hi')" in texts[0]


# --------------------------------------------------------------------- the proxy


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        ({"messages": [{"role": "user", "content": "vc off"}]}, True),
        ({"messages": [{"role": "user", "content": [{"type": "text", "text": "vc"}]}]}, True),
        (
            {
                "messages": [
                    {"role": "user", "content": "vc off"},
                    {"role": "assistant", "content": "ok"},
                ]
            },
            False,
        ),
        (
            {
                "messages": [
                    {"role": "user", "content": "vc off"},
                    {"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
                ]
            },
            False,
        ),
        (
            {
                "messages": [
                    {"role": "user", "content": "vc off"},
                    {"role": "system", "content": [{"type": "text", "text": "# Environment"}]},
                ]
            },
            True,
        ),
        ({"input": [{"role": "user", "content": [{"type": "input_text", "text": "vc"}]}]}, True),
        ({"input": [{"type": "function_call_output", "output": "x"}]}, False),
        ({"contents": [{"role": "user", "parts": [{"text": "vc"}]}]}, True),
        ({"contents": [{"role": "model", "parts": [{"text": "vc"}]}]}, False),
        ({"model": "x"}, False),
    ],
)
def test_only_the_newest_message_counts(doc: dict[str, Any], expected: bool) -> None:
    assert ends_with_the_users_words(doc) is expected


@pytest.mark.parametrize(
    ("provider", "path", "yes"),
    [
        ("anthropic", "v1/messages", True),
        ("anthropic", "v1/messages/count_tokens", False),
        ("openai", "v1/chat/completions", True),
        ("openai-chatgpt", "responses", True),
        ("openai", "v1/models", False),
        ("ollama", "api/chat", True),
        ("ollama", "api/tags", False),
        ("gemini", "v1beta/models/x:streamGenerateContent", True),
        ("gemini", "v1beta/models", False),
    ],
)
def test_only_model_calls_are_answered(provider: str, path: str, yes: bool) -> None:
    assert is_a_model_call(provider, path) is yes


def _body(reply: Any) -> str:
    if hasattr(reply, "body_iterator"):

        async def collect() -> bytes:
            return b"".join([c async for c in reply.body_iterator])

        return asyncio.run(collect()).decode()
    return bytes(reply.body).decode()


@pytest.mark.parametrize(
    ("provider", "path", "doc", "needles"),
    [
        (
            "anthropic",
            "v1/messages",
            {"stream": True},
            ["message_start", "text_delta", "Hello", "end_turn", "message_stop"],
        ),
        ("anthropic", "v1/messages", {}, ['"type": "message"', "Hello"]),
        ("openai", "v1/chat/completions", {"stream": True}, ["chunk", "Hello", "[DONE]"]),
        ("openai", "v1/chat/completions", {}, ["chat.completion", "Hello"]),
        (
            "openai-chatgpt",
            "responses",
            {"stream": True},
            ["response.created", "response.output_text.delta", "Hello", "response.completed"],
        ),
        ("openai", "v1/responses", {}, ['"status": "completed"', "Hello"]),
        ("ollama", "api/chat", {}, ['"done": true', "Hello"]),
        ("gemini", "v1beta/models/x:streamGenerateContent", {}, ["candidates", "Hello", "STOP"]),
    ],
)
def test_each_provider_gets_a_reply_in_its_own_format(
    provider: str, path: str, doc: dict[str, Any], needles: list[str]
) -> None:
    body = _body(synthetic_reply(provider, path, doc, "Hello"))
    for needle in needles:
        assert needle in body


def _proxy(*, narration_off: bool = False) -> tuple[TestClient, ChatCommands, list[str]]:
    bus = EventBus()
    commands = ChatCommands(bus=bus, panel_url="http://panel/", language=lambda: "en")
    commands.narration_off = narration_off
    ran: list[str] = []

    class Center:
        async def run(self, verb: str, args: list[str]) -> dict[str, str]:
            ran.append(verb)
            return {"text": f"did {verb}"}

    commands.center = Center()
    return TestClient(create_proxy_app(bus, commands=commands)), commands, ran


def test_the_proxy_answers_a_command_and_calls_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, ran = _proxy()

    async def no_upstream(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("this must not reach the model")

    monkeypatch.setattr("voice_copilot.proxy.server._forward", no_upstream)
    reply = client.post(
        "/anthropic/v1/messages",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": "<system-reminder>be nice</system-reminder>\nvc check",
                }
            ],
        },
    )
    assert "did check" in reply.text
    assert ran == ["check"]


def test_the_proxy_runs_a_repeated_command_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, ran = _proxy()
    body = {"messages": [{"role": "user", "content": "vc check"}]}
    client.post("/anthropic/v1/messages", json=body)
    client.post("/anthropic/v1/messages", json=body)
    assert ran == ["check"]


def test_the_proxy_passes_what_only_mentions_a_command(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, ran = _proxy()
    forwarded: list[str] = []

    async def upstream(*_a: Any, **_k: Any) -> Any:
        from fastapi import Response

        forwarded.append("yes")
        return Response(content=b"{}")

    monkeypatch.setattr("voice_copilot.proxy.server._forward", upstream)
    client.post(
        "/anthropic/v1/messages",
        json={"messages": [{"role": "user", "content": "what does vc off do?"}]},
    )
    assert forwarded == ["yes"]
    assert ran == []


def test_off_in_the_proxy_stops_narrating_and_keeps_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, commands, _ = _proxy()
    client.post(
        "/anthropic/v1/messages", json={"messages": [{"role": "user", "content": "vc off"}]}
    )
    assert commands.narration_off is True

    seen: list[Any] = []

    async def upstream(_req: Any, *, parser_factory: Any, **_k: Any) -> Any:
        from fastapi import Response

        seen.append(parser_factory)
        return Response(content=b"{}")

    monkeypatch.setattr("voice_copilot.proxy.server._forward", upstream)
    client.post("/anthropic/v1/messages", json={"messages": [{"role": "user", "content": "hi"}]})
    assert seen == [None]

    client.post("/anthropic/v1/messages", json={"messages": [{"role": "user", "content": "vc on"}]})
    assert commands.narration_off is False


def test_the_plugins_are_found_with_or_without_a_checkout() -> None:
    from voice_copilot.companion.setup import bundle_root

    root = bundle_root()
    assert (root / "plugins" / "pi" / "voice-copilot.ts").is_file()
    assert (root / ".claude-plugin" / "marketplace.json").is_file()
    assert (root / ".agents" / "plugins" / "marketplace.json").is_file()
