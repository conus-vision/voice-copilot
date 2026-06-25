# Manual verification — vc launch wrapper (Plans 1–3)

Everything automatable is already green (54 tests, ruff, mypy on 63 files,
both default and `--platform linux`). These checks need a **real interactive
terminal** (a Windows Terminal / PowerShell window — not an IDE output pane),
because they exercise raw-console I/O, OS window focus, push-to-talk, and
PATH pickup, none of which a non-interactive shell or unit test can drive.

Run from the worktree: `F:\_VOICE_COPILOT\.claude\worktrees\vc-launch-core`
(or wherever this branch is checked out), via `uv run voice-copilot ...`.

## Plan 1 — launch core (PtyAdapter)

1. **Live terminal + clean exit:** `uv run voice-copilot vc cmd`
   - You should get a normal `cmd` prompt you can type into (proves the
     ConPTY raw-mode passthrough). Type `echo hi`, see `hi`. `exit` →
     the whole `vc` process should exit on its own.
2. **Push-to-talk injection:** with that `vc cmd` session focused, press
   **Alt+Space**, speak a short phrase, release. The transcribed text should
   appear as typed input in the `cmd` session. (Exercises stdin injection
   while the pump runs — and the write-lock added in review.)
3. **Real catalog CLI (if installed/authed):** `uv run voice-copilot vc claude`
   - Browser panel opens; Claude's normal TUI renders exactly as `claude`
     alone; narration plays in the panel once Claude does something.

## Plan 2 — vc alias

4. **Alias install (fresh shell):** in a terminal where `vc` is NOT yet a
   command (`Get-Command vc` fails), run `uv run voice-copilot version`. Open
   a **new** terminal, run `vc version` → same output as `voice-copilot
   version`. (PATH changes don't reach already-open shells, hence "new".)
5. **Doesn't clobber an existing `vc`:** if you already had a `vc` on PATH,
   confirm it still resolves to the original tool, untouched.

## Plan 3 — focus router + narrate-only-when-focused

6. **Checked (default), single instance:** `vc claude`, give it work so it
   narrates. Switch focus to an unrelated app (text editor) → narration
   stops; switch back → resumes. (Live `current_focus` gate.)
7. **Unchecked, single instance:** in the panel's **Settings** tab, uncheck
   "Narrate only when focused". Focus the instance once, then switch to an
   unrelated app → narration **keeps** going (sticky). NB: per design,
   unchecked is silent until you've focused the instance at least once.
8. **Two instances, checked:** run `vc claude` and `vc codex` in two
   terminals; trigger narration in both. Only the focused one should be
   audible; switching focus switches who speaks (≤0.5s lag).
9. **Two instances, unchecked:** uncheck the box in both. Focus A, let it
   narrate, switch to an unrelated app → A keeps narrating, B silent. Focus
   B's terminal → narration switches to B, A goes silent. (Cross-process
   `focus-state.json` arbitration.)
10. **Single push-to-talk → single recording** (final-review note #1): one
    Alt+Space while the panel is focused should yield exactly one mic
    recording, no flicker/double-trigger.

Record pass/fail per item. Anything that fails → tell me the symptom and
I'll dig in.
