import pytest

from voice_copilot.providers.llm.auto import AutoCommentatorProvider
from voice_copilot.providers.llm.base import LLMMessage


@pytest.mark.asyncio
async def test_unset_cli_raises_clear_error() -> None:
    provider = AutoCommentatorProvider(cli=None, binary=None)
    with pytest.raises(RuntimeError, match="no launched CLI"):
        async for _ in provider.stream_chat([LLMMessage(role="user", content="hi")]):
            pass


@pytest.mark.asyncio
async def test_unsupported_cli_raises(monkeypatch) -> None:
    provider = AutoCommentatorProvider(cli="totally-unknown", binary="/usr/bin/x")
    with pytest.raises(RuntimeError, match="no narration profile"):
        async for _ in provider.stream_chat([LLMMessage(role="user", content="hi")]):
            pass


@pytest.mark.asyncio
async def test_yields_cli_output(monkeypatch) -> None:
    captured = {}

    def fake_run_cli(cmd, *, stdin_text=None, timeout=60.0):
        captured["cmd"] = cmd
        captured["stdin"] = stdin_text
        return ("we are reading the file", "")

    monkeypatch.setattr("voice_copilot.providers.llm.auto.run_cli", fake_run_cli)
    provider = AutoCommentatorProvider(cli="claude", binary="/usr/bin/claude")

    chunks = [c async for c in provider.stream_chat([LLMMessage(role="user", content="narrate")])]
    assert "".join(chunks) == "we are reading the file"
    assert captured["cmd"][0] == "/usr/bin/claude"
    assert "--model" in captured["cmd"]
