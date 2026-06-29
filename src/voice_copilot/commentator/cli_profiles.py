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
    input_mode: str  # "stdin" | "arg"
    model: str


NARRATION_PROFILES: dict[str, NarrationProfile] = {
    # copilot reads the prompt from stdin (interactive); -p triggers agent mode.
    "copilot": NarrationProfile(
        args=["--allow-all", "--no-auto-update", "-s"],
        input_mode="stdin",
        model="gpt-5-mini",
    ),
    # claude -p runs the agent; force a cheap model and disable tools so it
    # narrates instead of acting.
    "claude": NarrationProfile(
        args=["-p", "--allowedTools", ""],
        input_mode="arg",
        model="claude-haiku-4-5-20251001",
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


def build_narration_command(
    cli: str, binary: str, prompt: str, *, model: str | None = None
) -> tuple[list[str], str | None]:
    """Return (argv, stdin_text) for a one-shot narration completion.

    Raises KeyError if `cli` has no narration profile.
    """
    profile = NARRATION_PROFILES[cli]
    chosen_model = model or profile.model
    argv = [binary, "--model", chosen_model, *profile.args]
    if profile.input_mode == "stdin":
        return argv, prompt
    argv.append(prompt)
    return argv, None
