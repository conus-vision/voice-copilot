import asyncio
import sys
import time

import pytest

from voice_copilot.providers.llm._cli_runner import build_flat_prompt, run_cli
from voice_copilot.providers.llm.base import LLMMessage


async def test_run_cli_feeds_stdin_and_captures_stdout() -> None:
    # Echo stdin back out via a tiny python program — cross-platform.
    cmd = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"]
    out, _ = await run_cli(cmd, stdin_text="hello")
    assert out == "HELLO"


async def test_run_cli_without_stdin_runs_arg_mode() -> None:
    cmd = [sys.executable, "-c", "print('from-args')"]
    out, _ = await run_cli(cmd)
    assert out.strip() == "from-args"


async def test_run_cli_without_stdin_gives_the_child_no_terminal() -> None:
    # Under `vc` the terminal belongs to the wrapped agent; a narrator child
    # reading from it would steal the user's keystrokes.
    cmd = [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"]
    out, _ = await run_cli(cmd)
    assert out.strip() == "''"


async def test_run_cli_times_out() -> None:
    cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
    with pytest.raises(RuntimeError, match="timeout"):
        await run_cli(cmd, timeout=0.3)


async def test_cancelling_run_cli_does_not_wait_for_the_child() -> None:
    cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
    task = asyncio.create_task(run_cli(cmd, timeout=60.0))
    await asyncio.sleep(0.3)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 3


def test_build_flat_prompt_joins_system_and_user() -> None:
    prompt = build_flat_prompt(
        "SYS", [LLMMessage(role="user", content="hi"), LLMMessage(role="assistant", content="prev")]
    )
    assert "SYS" in prompt
    assert "hi" in prompt
    assert "[assistant]: prev" in prompt
