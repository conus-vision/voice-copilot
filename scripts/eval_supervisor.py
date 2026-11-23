"""Score the supervisor prompt against checkpoints with a known right answer.

The prompts are product surface, and a change that reads better can judge worse: an
early draft of today's prompt cut false alarms and missed real trouble instead. Run
this before and after editing one, through the same provider and parser as the app:

    uv run python scripts/eval_supervisor.py src/voice_copilot/commentator/prompts/supervisor.md --language ru gemma4:12b
    uv run python scripts/eval_supervisor.py src/voice_copilot/commentator/prompts/supervisor.md cli:claude:sonnet --repeats 1

A model is an Ollama name, or cli:<cli>:<model> to go through a CLI's own login.

`--live` reviews the way a running conversation does (cli:claude only): a checkpoint is
split in two, the first half is the conversation's opening message, and the second half
arrives as "new since your last verdict" and is the one scored. `--set sequences` adds
reviews in a row that a single checkpoint can't show (scripts/supervisor_sequences.py);
run them one-shot and live on the same model to compare.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from supervisor_checkpoints import CASES
from supervisor_sequences import SEQUENCES

from voice_copilot.commentator.cli_profiles import NARRATION_PROFILES
from voice_copilot.commentator.format import build_supervisor_delta, build_supervisor_user
from voice_copilot.commentator.pipeline import parse_supervisor_verdict
from voice_copilot.commentator.prompts import fill
from voice_copilot.providers import registry
from voice_copilot.providers.llm.base import LLMMessage
from voice_copilot.providers.llm.live_claude import LiveClaude

_OPENING_REASON = "8 tool calls since the last review"


def _provider(model: str) -> tuple[Any, str]:
    if model.startswith("cli:"):
        _, cli, name = model.split(":", 2)
        options = {"cli": cli, "binary": shutil.which(cli), "model": name}
        return registry.build("llm", "auto", options), "cli"
    return registry.build("llm", "openai-compat", {"model": model}), "api"


class _Reviewer:
    """One-shot calls, or one live conversation: the same two calls either way."""

    def __init__(self, model: str, prompt: str, language: str, live: bool) -> None:
        self.llm, self.style = _provider(model)
        self.system = fill(prompt, language, self.style)
        self.live: LiveClaude | None = None
        if live:
            if not model.startswith("cli:claude:"):
                raise SystemExit("--live runs through the Claude CLI: use cli:claude:<model>")
            self.live = LiveClaude(
                shutil.which("claude") or "claude",
                system=self.system,
                model=model.split(":", 2)[2],
                args=NARRATION_PROFILES["claude"].args,
            )

    async def review(self, full: str, delta: str) -> tuple[str, str]:
        if self.live is not None:
            reply = (await self.live.turn(delta if self.live.turns else full)).text
        else:
            pieces = [
                d
                async for d in self.llm.stream_chat(
                    [LLMMessage(role="user", content=full)],
                    system=self.system,
                    max_tokens=200,
                    temperature=0.2,
                )
            ]
            reply = "".join(pieces)
        if not reply.strip():
            # parse_supervisor_verdict reads this as OK, which would pass for a right answer.
            raise RuntimeError("empty reply")
        return parse_supervisor_verdict(reply)

    async def close(self) -> None:
        if self.live is not None:
            await self.live.close()


@dataclass
class _Score:
    right: int = 0
    total: int = 0
    failed: int = 0
    missed: int = 0
    false_alarms: int = 0
    false_stops: int = 0
    seconds: float = 0.0
    lines: list[str] = field(default_factory=list)

    def add(self, status: str, want: set[str]) -> None:
        self.total += 1
        self.right += status in want
        self.missed += status == "ok" and "ok" not in want
        self.false_alarms += status != "ok" and want == {"ok"}
        self.false_stops += status == "stop" and "stop" not in want


def _full(goal: str, lines: list[str], reason: str, language: str, **extra: Any) -> str:
    return build_supervisor_user(
        user_query=goal,
        summary=None,
        history=lines[-80:],
        reason=reason,
        language=language,
        **extra,
    )


def _delta(lines: list[str], reason: str, language: str, extra: dict[str, Any]) -> str:
    return build_supervisor_delta(
        new_lines=lines,
        reason=reason,
        progress=extra.get("progress"),
        already_said=extra.get("already_said"),
        edits=extra.get("edits"),
        language=language,
    )


async def _case(args: argparse.Namespace, prompt: str, case: dict[str, Any], score: _Score) -> None:
    want: set[str] = case["want"]
    lines = case["events"].strip().splitlines()
    extra = case.get("extra", {})
    got: list[str] = []
    for _ in range(args.repeats):
        reviewer = _Reviewer(args.model, prompt, args.language, args.live)
        try:
            if args.live:
                half = max(1, len(lines) // 2)
                opening = _full(case["goal"], lines[:half], _OPENING_REASON, args.language)
                await reviewer.review(opening, "")
                status, message = await reviewer.review(
                    "", _delta(lines[half:], case["reason"], args.language, extra)
                )
            else:
                full = _full(case["goal"], lines, case["reason"], args.language, **extra)
                status, message = await reviewer.review(full, "")
        except Exception as exc:
            score.failed += 1
            print(f"    {case['id']}: call failed: {str(exc)[:160]}")
            continue
        finally:
            await reviewer.close()
        got.append(status)
        score.add(status, want)
        if args.show and message:
            print(f"    {case['id']} {status.upper()}: {message}")
    mark = "ok " if got and all(s in want for s in got) else "~  " if set(got) & want else "BAD"
    print(f"{mark} {case['id']:44} want={'/'.join(sorted(want)):10} got={' '.join(got)}")


async def _sequence(
    args: argparse.Namespace, prompt: str, seq: dict[str, Any], score: _Score
) -> None:
    reviewer = _Reviewer(args.model, prompt, args.language, args.live)
    seen: list[str] = []
    told: str | None = None
    got: list[str] = []
    try:
        for i, turn in enumerate(seq["turns"]):
            new = turn["events"].strip().splitlines()
            seen += new
            extra = {"already_said": told} if told else {}
            full = _full(seq["goal"], seen, turn["reason"], args.language, **extra)
            delta = _delta(new, turn["reason"], args.language, extra)
            try:
                status, message = await reviewer.review(full, delta)
            except Exception as exc:
                score.failed += 1
                print(f"    {seq['id']}#{i + 1}: call failed: {str(exc)[:160]}")
                return
            if status != "ok" and message:
                told = message  # what the pipeline would have spoken
            want = turn.get("want")
            mark = "-"
            if want:
                score.add(status, want)
                mark = "ok" if status in want else "BAD"
            got.append(f"{status}{'' if mark in ('ok', '-') else '!'}")
            if args.show and message:
                print(f"    {seq['id']}#{i + 1} {status.upper()}: {message}")
    finally:
        await reviewer.close()
    wants = " ".join("/".join(sorted(t.get("want") or {"-"})) for t in seq["turns"])
    bad = any(g.endswith("!") for g in got)
    print(f"{'BAD' if bad else 'ok '} {seq['id']:44} want={wants}  got={' '.join(got)}")


async def _main(args: argparse.Namespace) -> None:
    prompt = args.prompt.read_text(encoding="utf-8").strip()
    score = _Score()
    started = time.monotonic()
    if args.set in ("cases", "all"):
        for case in CASES:
            await _case(args, prompt, case, score)
    if args.set in ("sequences", "all"):
        for seq in SEQUENCES:
            await _sequence(args, prompt, seq, score)
    per_call = (time.monotonic() - started) / max(score.total, 1)
    mode = "live" if args.live else "one-shot"
    print(
        f"== {args.prompt.name} [{args.model}, {args.language}, {mode}, {args.set}]: "
        f"{score.right}/{score.total} right, {score.missed} missed alerts, "
        f"{score.false_alarms} false alarms, {score.false_stops} false STOPs, "
        f"{per_call:.1f} s per scored review, {score.failed} failed calls"
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
    parser.add_argument("--live", action="store_true", help="review as a running conversation")
    parser.add_argument("--set", choices=["cases", "sequences", "all"], default="cases")
    asyncio.run(_main(parser.parse_args()))
