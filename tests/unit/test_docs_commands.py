"""Every command the README and the docs tell people to type exists.

A renamed subcommand, a dropped flag or a CLI id missing from the catalog
would otherwise surface only when someone pastes the line into a terminal.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
import pytest
import typer

from voice_copilot import cli
from voice_copilot.companion import integrations
from voice_copilot.proxy.cli_catalog import CLI_CATALOG

_ROOT = Path(__file__).resolve().parents[2]
_DOCS = [_ROOT / "README.md", *sorted((_ROOT / "docs").glob("*.md"))]

_FENCE = re.compile(r"^```(\w*)\n(.*?)^```", re.MULTILINE | re.DOTALL)
_INLINE = re.compile(r"`([^`\n]+)`")
_SHELL_FENCES = {"", "bash", "sh", "shell", "console", "powershell"}
#: `vc` is a shim for `voice-copilot`; both start a command line.
_PROGRAMS = ("voice-copilot", "vc")
_RUN_TARGETS = {"claude", "codex"}


def _command_lines() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for doc in _DOCS:
        text = doc.read_text(encoding="utf-8")
        name = doc.relative_to(_ROOT).as_posix()
        for lang, body in _FENCE.findall(text):
            if lang in _SHELL_FENCES:
                found += [(name, line) for line in body.splitlines()]
        prose = _FENCE.sub("", text)
        found += [(name, span) for span in _INLINE.findall(prose)]
    commands = []
    for name, line in found:
        line = line.strip().removeprefix("$ ")
        if line.split(" ", 1)[0] in _PROGRAMS:
            commands.append((name, line))
    return commands


_COMMANDS = _command_lines()
_GROUP = typer.main.get_command(cli._TYPER_APP)
assert isinstance(_GROUP, click.Group)


def _options(command: click.Command) -> set[str]:
    names = {"--help"}
    for param in command.params:
        names.update(param.opts)
        names.update(param.secondary_opts)
    return names


def test_the_docs_show_commands_at_all() -> None:
    # Guards the extraction itself: an empty list would pass every check.
    assert len(_COMMANDS) > 20
    assert any(line.startswith("voice-copilot integrate") for _, line in _COMMANDS)


@pytest.mark.parametrize(("doc", "line"), _COMMANDS, ids=[line for _, line in _COMMANDS])
def test_documented_command_exists(doc: str, line: str) -> None:
    tokens = shlex.split(line, comments=True)[1:]
    if not tokens or tokens[0].startswith("-"):
        return
    # An unknown first word is a CLI name: `voice-copilot claude` runs `vc claude`.
    if tokens[0] in _GROUP.commands:
        sub, args = tokens[0], tokens[1:]
    else:
        sub, args = "vc", tokens
    command = _GROUP.commands[sub]
    passes_through = bool(command.context_settings.get("ignore_unknown_options"))

    positionals: list[str] = []
    for token in args:
        if token.startswith("<") or token == "...":
            break  # a placeholder such as <cli>
        if token.startswith("-"):
            if passes_through and positionals:
                break  # handed to the launched CLI, e.g. `vc claude --resume`
            option = token.split("=", 1)[0]
            assert option in _options(command), f"{doc}: `{line}`: {sub} has no {option}"
        else:
            positionals.append(token)

    if not positionals:
        return
    target = positionals[0]
    if sub == "vc":
        commands = {entry.command for entry in CLI_CATALOG.values()}
        assert target in CLI_CATALOG or target in commands, f"{doc}: `{line}`: no CLI {target!r}"
    elif sub == "integrate":
        known = {integration.id for integration in integrations.all_integrations()}
        assert target in known, f"{doc}: `{line}`: no integration {target!r}"
    elif sub == "run":
        assert target in _RUN_TARGETS, f"{doc}: `{line}`: run cannot wrap {target!r}"
