"""The live `claude -p`: one process, turn after turn, and never one left half-way."""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path

import pytest

from voice_copilot.providers.llm.live_claude import LiveClaude

_FAKE = textwrap.dedent(
    """
    import json, os, sys, time
    n = 0
    for line in sys.stdin:
        content = json.loads(line)["message"]["content"]
        n += 1
        if content == "die":
            sys.stderr.write("rate limit reached\\n")
            sys.exit(3)
        if content == "slow":
            time.sleep(30)
        print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
        usage = {"input_tokens": 3, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 1000 * n}
        print(json.dumps({"type": "result", "result": f"OK {n} pid={os.getpid()}", "usage": usage}), flush=True)
    """
)


@pytest.fixture
def fake_claude(tmp_path: Path) -> str:
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE, encoding="utf-8")
    if os.name == "nt":
        wrapper = tmp_path / "claude.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        wrapper = tmp_path / "claude"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        wrapper.chmod(0o755)
    return str(wrapper)


def _live(binary: str) -> LiveClaude:
    return LiveClaude(binary, system="judge", model="opus", args=["--tools", "", "-p"])


async def test_turns_share_one_process_and_report_the_cache(fake_claude: str) -> None:
    live = _live(fake_claude)
    first = await live.turn("first review")
    second = await live.turn("what is new")
    await live.close()
    assert first.text.startswith("OK 1") and second.text.startswith("OK 2")
    assert first.text.split("pid=")[1] == second.text.split("pid=")[1]
    assert second.usage["cache_read_input_tokens"] == 2000
    assert live.context_tokens == 3 + 50 + 2000
    assert live.turns == 2


async def test_a_dead_process_says_why_and_the_next_turn_starts_fresh(fake_claude: str) -> None:
    live = _live(fake_claude)
    await live.turn("first")
    with pytest.raises(RuntimeError, match="rate limit reached"):
        await live.turn("die")
    assert not live.alive
    again = await live.turn("first again")
    await live.close()
    assert again.text.startswith("OK 1")  # a new process, a new conversation


async def test_a_turn_cut_short_is_never_reused(fake_claude: str) -> None:
    live = _live(fake_claude)
    first = await live.turn("first")
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(1.0):
            await live.turn("slow")
    assert not live.alive
    after = await live.turn("next")
    await live.close()
    assert after.text.startswith("OK 1")
    assert after.text.split("pid=")[1] != first.text.split("pid=")[1]
