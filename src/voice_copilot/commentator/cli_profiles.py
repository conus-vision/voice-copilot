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
    # What the supervisor uses when the user hasn't picked a model: the
    # capable tier of the same CLI, so "reuse the CLI I launched" still holds.
    strong_model: str | None = None
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
        strong_model="sonnet",
        system_file_flag="--system-prompt-file",
    ),
    # `exec` alone inherits the user's whole codex setup — their heavyweight
    # model, `reasoning_effort = ultra`, MCP servers, plugins — which makes a
    # two-sentence narration slow and noisy. The flags strip all of that: no
    # user config, no session file written, no ANSI, and the banner goes to
    # stderr so stdout is just the sentence. The model must be one a ChatGPT
    # plan accepts (an API-only slug is rejected outright); override it per-CLI
    # in the Commentator tab if this one isn't on your plan.
    "codex": NarrationProfile(
        args=[
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
        ],
        # stdin, not a positional arg: `codex` on Windows is a .cmd wrapper, and
        # the batch layer mangles a long multi-line Cyrillic prompt so badly
        # that codex answers something unrelated — the narration came back as
        # invented prose about a quiet evening while the summary call reported
        # "no input data was provided". `codex exec` reads the prompt from
        # stdin when no positional one is given.
        input_mode="stdin",
        model="gpt-5.4-mini",
        strong_model="gpt-5.5",
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
        strong_model="gemini-2.5-pro",
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


def strong_model_for(cli: str | None) -> str | None:
    """The supervisor's default model for `cli`, or None if it has no profile."""
    profile = NARRATION_PROFILES.get(cli or "")
    return profile.strong_model if profile is not None else None
