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
async def test_a_refused_model_is_named_with_what_to_do(monkeypatch) -> None:
    # The supervisor calls this provider too, so the words don't say "narrated".
    refusal = (
        'ERROR: {"type":"error","status":400,"error":{"message":"The \'gpt-6.2-sol\' model is '
        'not supported when using Codex with a ChatGPT account.","code":"model_not_found"}}'
    )

    async def fake_run_cli(cmd, *, stdin_text=None, timeout=60.0):
        return ("", f"warning: something\n{refusal}\n")

    monkeypatch.setattr("voice_copilot.providers.llm.auto.run_cli", fake_run_cli)
    provider = AutoCommentatorProvider(cli="codex", binary="/bin/codex", model="gpt-6.2-sol")
    with pytest.raises(RuntimeError) as caught:
        async for _ in provider.stream_chat([LLMMessage(role="user", content="review")]):
            pass
    message = str(caught.value)
    assert message.startswith("codex (gpt-6.2-sol) gave no answer — ERROR:")
    assert "model_not_found" in message
    assert message.endswith("Pick another model in Settings.")
    assert "narrated" not in message


@pytest.mark.asyncio
async def test_yields_cli_output(monkeypatch) -> None:
    captured = {}

    async def fake_run_cli(cmd, *, stdin_text=None, timeout=60.0):
        captured["cmd"] = cmd
        captured["stdin"] = stdin_text
        return ("we are reading the file", "")

    monkeypatch.setattr("voice_copilot.providers.llm.auto.run_cli", fake_run_cli)
    provider = AutoCommentatorProvider(cli="claude", binary="/usr/bin/claude")

    chunks = [c async for c in provider.stream_chat([LLMMessage(role="user", content="narrate")])]
    assert "".join(chunks) == "we are reading the file"
    assert captured["cmd"][0] == "/usr/bin/claude"
    assert "--model" in captured["cmd"]
