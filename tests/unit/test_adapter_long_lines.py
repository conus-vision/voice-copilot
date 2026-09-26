"""A stream-json line past asyncio's 64 KiB default must not stop the reader.

One tool result (a whole file read back, a long command's output) is easily
bigger than that. `readline()` used to raise, the reader task died, nothing
drained the pipe any more and the wrapped CLI blocked on its next write.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest

from voice_copilot.adapters import ClaudeCodeAdapter, CodexAdapter
from voice_copilot.core.bus import EventBus
from voice_copilot.core.events import EventKind

# Built inside the child: a 200 KB literal would exceed the OS limit on one argv item.
_CLAUDE_SCRIPT = """
import json
text = "x" * 200_000
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success"}), flush=True)
"""

_CODEX_SCRIPT = """
import json
item = {"type": "command_execution", "id": "c1", "exit_code": 0, "aggregated_output": "x" * 200_000}
print(json.dumps({"type": "item.completed", "item": item}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {}}), flush=True)
"""


async def _kinds_until_turn_end(adapter: Any, bus: EventBus, prompt: str | None) -> list[str]:
    kinds: list[str] = []
    async with bus.subscribe() as q:
        await adapter.start(initial_prompt=prompt)
        try:
            while True:
                ev = await asyncio.wait_for(q.get(), timeout=10)
                kinds.append(ev.kind.value)
                if ev.kind is EventKind.TURN_ENDED:
                    break
        finally:
            await adapter.stop()
    return kinds


@pytest.mark.parametrize(
    ("adapter_cls", "script", "prompt", "expected"),
    [
        (ClaudeCodeAdapter, _CLAUDE_SCRIPT, None, EventKind.AGENT_TEXT),
        (CodexAdapter, _CODEX_SCRIPT, "hi", EventKind.TOOL_CALL_FINISHED),
    ],
    ids=["claude", "codex"],
)
async def test_reader_survives_a_line_over_64_kib(
    monkeypatch: pytest.MonkeyPatch,
    adapter_cls: Any,
    script: str,
    prompt: str | None,
    expected: EventKind,
) -> None:
    real_exec = asyncio.create_subprocess_exec

    async def run_script_instead(*_argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        # Same pipes and limit the adapter asked for; only the program differs.
        return await real_exec(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", run_script_instead)
    bus = EventBus()
    kinds = await _kinds_until_turn_end(adapter_cls(bus, binary=sys.executable), bus, prompt)
    assert expected.value in kinds
    assert kinds[-1] == EventKind.TURN_ENDED.value


def test_claude_adapter_runs_in_print_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # --input-format/--output-format are print-mode flags (Claude Code CLI reference).
    seen: list[tuple[str, ...]] = []

    async def record(*argv: str, **_: Any) -> Any:
        seen.append(argv)
        raise RuntimeError("stop here")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    adapter = ClaudeCodeAdapter(EventBus(), binary=sys.executable)
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(adapter.start())
    assert seen and seen[0][1] == "-p"
    assert "stream-json" in seen[0]
    json.dumps(seen)  # argv stays plain strings
