"""Score the supervisor prompt against checkpoints with a known right answer.

The prompts are product surface, and a change that reads better can judge worse: an
early draft of today's prompt cut false alarms and missed real trouble instead. Run
this before and after editing one, through the same provider and parser as the app:

    uv run python scripts/eval_supervisor.py src/voice_copilot/commentator/prompts/supervisor.md --language ru gemma4:12b
    uv run python scripts/eval_supervisor.py src/voice_copilot/commentator/prompts/supervisor.md cli:claude:sonnet --repeats 1

A model is an Ollama name, or cli:<cli>:<model> to go through a CLI's own login.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from supervisor_checkpoints import CASES

from voice_copilot.commentator.format import build_supervisor_user
from voice_copilot.commentator.pipeline import parse_supervisor_verdict
from voice_copilot.commentator.prompts import fill
from voice_copilot.providers import registry
from voice_copilot.providers.llm.base import LLMMessage


def _provider(model: str) -> tuple[Any, str]:
    if model.startswith("cli:"):
        _, cli, name = model.split(":", 2)
        options = {"cli": cli, "binary": shutil.which(cli), "model": name}
        return registry.build("llm", "auto", options), "cli"
    return registry.build("llm", "openai-compat", {"model": model}), "api"


async def _review(
    llm: Any, style: str, system: str, case: dict[str, Any], language: str
) -> tuple[str, str]:
    user = build_supervisor_user(
        user_query=case["goal"],
        summary=None,
        history=case["events"].strip().splitlines(),
        reason=case["reason"],
        style=style,
        language=language,
        **case.get("extra", {}),
    )
    pieces = [
        delta
        async for delta in llm.stream_chat(
            [LLMMessage(role="user", content=user)],
            system=system,
            max_tokens=200,
            temperature=0.2,
        )
    ]
    reply = "".join(pieces)
    if not reply.strip():
        # parse_supervisor_verdict reads this as OK, which would pass for a right answer.
        raise RuntimeError("empty reply")
    return parse_supervisor_verdict(reply)


async def _main(args: argparse.Namespace) -> None:
    llm, style = _provider(args.model)
    system = fill(args.prompt.read_text(encoding="utf-8").strip(), args.language, style)
    right = total = failed = missed = false_alarms = false_stops = 0
    started = time.monotonic()
    for case in CASES:
        want = case["want"]
        got: list[str] = []
        for _ in range(args.repeats):
            try:
                status, message = await _review(llm, style, system, case, args.language)
            except Exception as exc:
                # One slow call says nothing about the prompt.
                failed += 1
                print(f"    {case['id']}: call failed: {str(exc)[:120]}")
                continue
            got.append(status)
            total += 1
            right += status in want
            missed += status == "ok" and "ok" not in want
            false_alarms += status != "ok" and want == {"ok"}
            false_stops += status == "stop" and "stop" not in want
            if args.show and message:
                print(f"    {case['id']} {status.upper()}: {message}")
        mark = "ok " if all(s in want for s in got) else "~  " if set(got) & want else "BAD"
        print(f"{mark} {case['id']:44} want={'/'.join(sorted(want)):10} got={' '.join(got)}")
    per_call = (time.monotonic() - started) / max(total, 1)
    print(
        f"== {args.prompt.name} [{args.model}, {args.language}]: {right}/{total} right, "
        f"{missed} missed alerts, {false_alarms} false alarms, {false_stops} false STOPs, "
        f"{per_call:.1f} s per call, {failed} failed calls"
    )


if __name__ == "__main__":
    # A Windows console's code page can't print the Russian cases and verdicts.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", type=Path)
    parser.add_argument("model")
    parser.add_argument("--language", default="en", help="the language it replies in")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--show", action="store_true", help="print what the model said")
    asyncio.run(_main(parser.parse_args()))
