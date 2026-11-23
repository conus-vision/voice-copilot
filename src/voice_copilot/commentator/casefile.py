"""The case file: what the code knows for certain about the request in hand.

The narrator's running summary is written by a small model for its own use: three
sentences about what was said. The end-of-work review and the advisor need facts
they can rely on instead, so this is kept by the code from the events: which checks
ran and how they ended, whether the code changed after the last one that passed,
what Supervisor+ held, and what the user has already been told.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from voice_copilot.core.danger import program_of, simple_commands

#: What a command checks, by the words that run it. Searched only in the program's own
#: words (`grep pytest` is a search, not a test run).
_CHECKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "tests",
        re.compile(
            r"\b(?:pytest|py\.test|unittest|nose2|tox|nox|jest|vitest|mocha|rspec|phpunit|ctest)\b"
            r"|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b"
            r"|\b(?:cargo|go|dotnet|mix|deno|swift)\s+test\b"
            r"|\b(?:gradlew?|mvn|make)\s+(?:\S+\s+)*test\b",
            re.IGNORECASE,
        ),
    ),
    ("types", re.compile(r"\b(?:mypy|pyright|basedpyright|tsc)\b", re.IGNORECASE)),
    (
        "lint",
        re.compile(
            r"\b(?:ruff|eslint|flake8|pylint|golangci-lint|clippy|rubocop|biome|stylelint)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "build",
        re.compile(
            r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build\b"
            r"|\b(?:cargo|go|dotnet)\s+build\b|\bgradlew?\s+build\b|\bmvn\s+(?:package|compile)\b",
            re.IGNORECASE,
        ),
    ),
)
_COMMAND_CHARS = 80


def check_kind(command: Any) -> str | None:
    """`tests`, `types`, `lint` or `build` if this shell command checks the code, else None."""
    if not isinstance(command, (str, list)):
        return None
    for words in simple_commands(command):
        if program_of(words) in ("grep", "rg", "findstr", "select-string", "echo", "cat"):
            continue
        text = " ".join(words)
        for kind, pattern in _CHECKS:
            if pattern.search(text):
                return kind
    return None


@dataclass
class Check:
    kind: str
    command: str
    runs: int = 0
    passed: bool | None = None
    #: Position in the request's events, to tell whether code changed after it.
    at: int = 0


@dataclass
class Note:
    """Something the advisor raised, and what the user did with it."""

    id: str
    text: str
    send: str | None = None
    status: str = "open"  # open | sent | dismissed


@dataclass
class CaseFile:
    #: Events seen in this request, as a clock: "after the last edit" compares these.
    step: int = 0
    last_edit_at: int = 0
    checks: dict[str, Check] = field(default_factory=dict)
    #: Checks started and not finished yet, by tool call id.
    running: dict[str, str] = field(default_factory=dict)
    held: list[str] = field(default_factory=list)
    told: list[str] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    #: The advisor's own list of what can go wrong in this task, kept between its calls.
    watch: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ recording

    def tick(self) -> None:
        self.step += 1

    def edited(self) -> None:
        self.last_edit_at = self.step

    def started(self, tool_id: str | None, command: Any) -> None:
        kind = check_kind(command)
        if kind is None:
            return
        text = command if isinstance(command, str) else " ".join(str(c) for c in command)
        check = self.checks.setdefault(kind, Check(kind=kind, command=text[:_COMMAND_CHARS]))
        check.command = text[:_COMMAND_CHARS]
        check.runs += 1
        check.passed = None
        check.at = self.step
        self.running[tool_id or f"_{self.step}"] = kind

    def finished(self, tool_id: str | None, failed: bool) -> None:
        key = tool_id if tool_id in self.running else next(reversed(self.running), None)
        if key is None:
            return
        kind = self.running.pop(key)
        self.checks[kind].passed = not failed

    # ------------------------------------------------------------------ reading

    @property
    def open_notes(self) -> list[Note]:
        return [n for n in self.notes if n.status == "open"]

    def render(self, *, goal: str | None, progress: str, files: list[str]) -> str:
        """The case file as the models read it: short lines, facts only."""
        lines = [f"- Task: {goal or '(not known)'}", f"- Work: {progress}"]
        if files:
            shown = ", ".join(files[:8]) + (f" (+{len(files) - 8} more)" if len(files) > 8 else "")
            lines.append(f"- Files edited: {shown}")
        if self.checks:
            for check in self.checks.values():
                outcome = {True: "passed", False: "FAILED", None: "result not seen"}[check.passed]
                when = (
                    "after the latest edit"
                    if check.at > self.last_edit_at
                    else "BEFORE the latest edit, so it doesn't cover it"
                )
                times = "once" if check.runs == 1 else f"{check.runs} times"
                lines.append(
                    f"- Check, {check.kind}: `{check.command}` ran {times}; last {outcome}, {when}."
                )
        elif files:
            lines.append("- Checks: no tests, type check, lint or build was run.")
        lines += [f"- Supervisor+ held: {h}" for h in self.held]
        lines += [f"- Already told the user: {t}" for t in self.told[-4:]]
        for note in self.notes:
            lines.append(f"- Advisor note ({note.status}): {note.text}")
        return "\n".join(lines)


_WATCH_ITEMS = 5


def parse_advice(text: str) -> tuple[list[str], str | None, str | None]:
    """(watch list, note, what to send the agent) from the advisor's reply.

    Anything that isn't a NOTE line is no note: an unreadable reply never interrupts
    the user. The watch list is kept even then, so the next call doesn't start over.
    """
    watch: list[str] = []
    note: str | None = None
    send: str | None = None
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*").strip()
        head, _, rest = line.partition(":")
        key = head.strip("*_` ").upper()
        rest = rest.strip().strip("*_` ").strip()
        if key == "WATCH" and rest:
            watch.append(rest[:120])
        elif key == "NOTE" and rest:
            note = rest
        elif key == "SEND" and rest and rest.lower().rstrip(".") not in ("none", "нет", "—", "-"):
            send = rest
    return watch[:_WATCH_ITEMS], note, send if note else None
