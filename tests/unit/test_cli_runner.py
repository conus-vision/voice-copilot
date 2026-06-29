import sys

from voice_copilot.providers.llm._cli_runner import build_flat_prompt, run_cli
from voice_copilot.providers.llm.base import LLMMessage


def test_run_cli_feeds_stdin_and_captures_stdout() -> None:
    # Echo stdin back out via a tiny python program — cross-platform.
    cmd = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"]
    out, _ = run_cli(cmd, stdin_text="hello")
    assert out == "HELLO"


def test_run_cli_without_stdin_runs_arg_mode() -> None:
    cmd = [sys.executable, "-c", "print('from-args')"]
    out, _ = run_cli(cmd)
    assert out.strip() == "from-args"


def test_run_cli_times_out() -> None:
    import pytest

    cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
    with pytest.raises(RuntimeError, match="timeout"):
        run_cli(cmd, timeout=0.3)


def test_build_flat_prompt_joins_system_and_user() -> None:
    prompt = build_flat_prompt(
        "SYS", [LLMMessage(role="user", content="hi"), LLMMessage(role="assistant", content="prev")]
    )
    assert "SYS" in prompt
    assert "hi" in prompt
    assert "[assistant]: prev" in prompt
