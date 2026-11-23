"""Score Supervisor+'s advisor prompt against checkpoints with a known right answer.

Like `eval_supervisor.py`: run it before and after editing the prompt, through the same
provider and parser as the app. A note where `ok` was wanted is a false alarm, the
reason people switch an advisor off; an `ok` where a note was wanted is a miss.

    uv run python scripts/eval_advisor.py src/voice_copilot/commentator/prompts/advisor.md cli:claude:sonnet --repeats 1
    uv run python scripts/eval_advisor.py src/voice_copilot/commentator/prompts/advisor.md gemma4:12b --language ru
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from advisor_checkpoints import CASES
from eval_supervisor import _provider

from voice_copilot.commentator.casefile import parse_advice
from voice_copilot.commentator.format import build_advisor_user
from voice_copilot.commentator.prompts import fill
from voice_copilot.providers.llm.base import LLMMessage


def _case_file(case: dict[str, Any]) -> str:
    lines = [f"- Task: {case['goal']}", "- Work: a few minutes since the request"]
    if case.get("files"):
        lines.append(f"- Files edited: {', '.join(case['files'])}")
    lines.append(case.get("checks") or "- Checks: no tests, type check, lint or build was run.")
    return "\n".join(lines)


async def _advise(
    llm: Any, system: str, case: dict[str, Any], language: str
) -> tuple[str, str | None, list[str]]:
    user = build_advisor_user(
        user_query=case["goal"],
        case_file=_case_file(case),
        watch=case.get("watch", []),
        raised=case.get("raised", []),
        reason=case["reason"],
        history=case["events"].strip().splitlines(),
        edits=case.get("edits"),
        language=language,
    )
    pieces = [
        delta
        async for delta in llm.stream_chat(
            [LLMMessage(role="user", content=user)], system=system, max_tokens=400, temperature=0.2
        )
    ]
    reply = "".join(pieces)
    if not reply.strip():
        raise RuntimeError("empty reply")
    watch, note, send = parse_advice(reply)
    return ("note" if note else "ok"), (f"{note} | SEND: {send}" if note else None), watch


async def _main(args: argparse.Namespace) -> None:
    llm, style = _provider(args.model)
    system = fill(args.prompt.read_text(encoding="utf-8").strip(), args.language, style)
    right = total = failed = missed = false_alarms = no_watch = 0
    started = time.monotonic()
    for case in CASES:
        if args.only and case["id"] not in args.only:
            continue
        want = case["want"]
        got: list[str] = []
        for _ in range(args.repeats):
            try:
                status, said, watch = await _advise(llm, system, case, args.language)
            except Exception as exc:
                failed += 1
                print(f"    {case['id']}: call failed: {str(exc)[:120]}")
                continue
            got.append(status)
            total += 1
            right += status in want
            missed += status == "ok" and "ok" not in want
            false_alarms += status == "note" and "note" not in want
            no_watch += not watch
            if args.show:
                if said:
                    print(f"    {case['id']} NOTE: {said}")
                print(f"    {case['id']} WATCH: {' / '.join(watch)}")
        mark = "ok " if all(s in want for s in got) else "~  " if set(got) & want else "BAD"
        print(f"{mark} {case['id']:38} want={'/'.join(sorted(want)):8} got={' '.join(got)}")
    per_call = (time.monotonic() - started) / max(total, 1)
    print(
        f"== {args.prompt.name} [{args.model}, {args.language}]: {right}/{total} right, "
        f"{missed} missed, {false_alarms} false alarms, {no_watch} without a watch list, "
        f"{per_call:.1f} s per call, {failed} failed calls"
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("prompt", type=Path)
    parser.add_argument("model")
    parser.add_argument("--language", default="en", help="the language it replies in")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--show", action="store_true", help="print what the model said")
    parser.add_argument("--only", nargs="+", help="score these case ids only")
    asyncio.run(_main(parser.parse_args()))
