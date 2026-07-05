"""How to invoke each supported CLI for a plain, cheap, tool-free narration
completion. Starting points; verified against the real binaries in manual
testing. A CLI that can't narrate cleanly is removed here so `auto` falls
back to the panel notice for it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NarrationProfile:
    args: list[str]
    input_mode: str  # "stdin" | "arg" — where the USER content goes
    model: str
    # If set, the narrator system prompt is passed inline via this flag.
    system_flag: str | None = None
    # If set, the system prompt is written to a temp file and passed via this
    # flag (path only). This dodges the Windows `.cmd` batch layer mangling a
    # big multi-line arg — the reason claude lost its prompt otherwise.
    system_file_flag: str | None = None


NARRATION_PROFILES: dict[str, NarrationProfile] = {
    # copilot reads the prompt from stdin (interactive); -p triggers agent mode.
    "copilot": NarrationProfile(
        args=["--allow-all", "--no-auto-update", "-s"],
        input_mode="stdin",
        model="gpt-5-mini",
    ),
    # claude -p is the full agent. Pass the narrator instructions via a temp
    # file (--system-prompt-file, so the multi-line prompt survives the .cmd
    # layer), drop the injected env/git/CLAUDE.md context, and feed the events
    # via stdin, so it narrates instead of answering the embedded question.
    "claude": NarrationProfile(
        args=["--exclude-dynamic-system-prompt-sections", "-p"],
        input_mode="stdin",
        model="claude-haiku-4-5-20251001",
        system_file_flag="--system-prompt-file",
    ),
    "codex": NarrationProfile(
        args=["exec"],
        input_mode="arg",
        model="gpt-5-mini",
    ),
    "opencode": NarrationProfile(
        args=["run"],
        input_mode="arg",
        model="github-copilot/gpt-5-mini",
    ),
    "gemini": NarrationProfile(
        args=["-p"],
        input_mode="arg",
        model="gemini-2.0-flash",
    ),
}


def profile_needs_system_file(cli: str) -> bool:
    """True if `cli`'s profile delivers the system prompt via a temp file.

    Raises KeyError if `cli` has no profile.
    """
    return NARRATION_PROFILES[cli].system_file_flag is not None


def _flatten(system: str | None, user: str) -> str:
    return "\n\n".join(p for p in (system, user) if p)


def build_narration_command(
    cli: str,
    binary: str,
    system: str | None,
    user: str,
    *,
    model: str | None = None,
    system_file_path: str | None = None,
) -> tuple[list[str], str | None]:
    """Return (argv, stdin_text) for a one-shot narration completion.

    System delivery, in priority order: `system_file_flag` (path passed in via
    `system_file_path`), else `system_flag` (inline), else flattened into the
    user prompt. `stdin_text` carries the user prompt for stdin-mode CLIs, else
    it is the final positional arg. Raises KeyError if `cli` has no profile.
    """
    profile = NARRATION_PROFILES[cli]
    chosen_model = model or profile.model
    argv = [binary, "--model", chosen_model]
    prompt = user
    if profile.system_file_flag:
        argv += [profile.system_file_flag, system_file_path or ""]
    elif profile.system_flag:
        argv += [profile.system_flag, system or ""]
    else:
        prompt = _flatten(system, user)
    argv += profile.args
    if profile.input_mode == "stdin":
        return argv, prompt
    argv.append(prompt)
    return argv, None
