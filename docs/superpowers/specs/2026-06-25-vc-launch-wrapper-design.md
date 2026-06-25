# `vc` / `voice-copilot run` launch wrapper

Status: approved design, not yet implemented.

## Goal

Reduce voice-copilot setup to a single command. Today, narrating a CLI
requires either running `voice-copilot serve` and manually exporting
`ANTHROPIC_BASE_URL` before launching the target CLI yourself, or relying on
a hardcoded headless stream-json adapter (no visible TUI). Neither matches
the desired UX:

```
claude              → plain claude terminal (baseline, unchanged)
voice-copilot claude → claude terminal + voice-copilot panel, narrating,
                       nothing to configure beyond installing voice-copilot
vc claude            → same, shorter to type
```

This spec covers the new launch path only. The existing `serve` command and
the existing headless stream-json adapters (`adapters/claude_code.py`,
`adapters/codex.py`) are unchanged — they remain the mechanism for headless/
batch use (`-p`/`exec` style invocations), which is a different use case
from "narrate my interactive session".

## Non-goals

- No ANSI/TUI-render parsing. If a CLI can't be intercepted via its network
  layer, voice-copilot does not attempt to guess its semantics from rendered
  terminal output. Per project convention, an unsupported case is surfaced
  loudly, not faked with a degraded heuristic.
- No central dashboard listing all running instances. Each `vc <cli>`
  invocation is independent; there is no cross-instance registry beyond
  what's needed for the focus router (below).
- No change to the existing headless adapters' wire protocol or behavior.

## CLI dispatch

`voice-copilot <name> [-- args...]` is the canonical form. Dispatch logic in
`cli.py`: if the first positional argument matches a registered subcommand
(`serve`, `version`, ...), route there as today; otherwise treat it as
`run <name>`, i.e. the name of a target CLI to launch and narrate.

`vc` is a convenience alias for the same dispatch, but is **not** declared as
a static `[project.scripts]` entry point — packaging metadata can't
conditionally skip registering a name based on what's already on the user's
PATH, and unconditionally registering `vc` risks silently shadowing or
conflicting with an unrelated tool the user already has.

Instead: on first invocation of `voice-copilot` (or an explicit
`voice-copilot install-alias` command), check `shutil.which("vc")`.
- Nothing found → create a thin forwarding shim next to the already-installed
  `voice-copilot` script (same `Scripts`/`bin` directory, already on PATH):
  `vc.exe`/`vc.cmd` on Windows, a symlink on Unix. The shim forwards all args
  to `voice-copilot`.
- Something found that isn't our own shim → leave it alone, do nothing, no
  message spam on every run (check once, remember the result e.g. via a
  marker file in the voice-copilot config dir so we don't re-probe every
  invocation).

## CLI profile registry

A registry, analogous to the existing pluggable-provider pattern in
`providers/`, maps a CLI name to how voice-copilot can intercept it:

```yaml
# ~/.voice-copilot/config.yaml
cli_profiles:
  claude:
    verified: true
    env: { ANTHROPIC_BASE_URL: "{proxy_url}" }
  codex:
    verified: true
    env: { OPENAI_BASE_URL: "{proxy_url}" }
  opencode:
    verified: false
    env: { OPENAI_BASE_URL: "{proxy_url}" }   # user-supplied, unconfirmed
```

`claude` and `codex` ship built in with `verified: true`. Users (or future
contributions) can add entries for other CLIs.

### Resolution per `vc <name>` invocation

1. **Tier 1 — known and verified** (`verified: true`): voice-copilot is
   certain of the env-var override and can rely on it working. Allocate a
   proxy port, spawn `<name>` in a PTY with that env var merged into the
   *child's* environment only (never mutates the voice-copilot process's own
   `os.environ`), open the browser panel. Fully automatic.
2. **Tier 2 — known but not verified** (entry exists, `verified: false`, or
   required fields are incomplete): voice-copilot isn't sure the mechanism
   works and can't confirm it in the background. Spawn `<name>` in a PTY
   *without* the env override, open the panel directly on a "Proxy settings
   for `<name>`" tab pre-filled with whatever is already known. Narration
   stays off until the user confirms/completes the settings there; applying
   them hot-reconfigures the already-running instance (no restart needed).
3. **Tier 3 — unknown** (no entry at all): spawn `<name>` as a plain PTY
   pass-through (terminal visible, no interception attempted), panel shows
   "this CLI isn't recognized" plus a short instruction for adding a
   `cli_profiles` entry. The CLI launches immediately; the instruction is
   informational, not a blocking prompt.

This tiering applies only to the `vc <cli>` interactive path. It has no
bearing on the existing headless adapters, which remain separate, hand-written
per-CLI parsers for CLIs whose own stream-json wire format we've chosen to
support for batch use.

## Components

1. **PTY process manager** — cross-platform child process bridge
   (ConPTY on Windows) wiring the child's stdin/stdout/stderr to the user's
   real terminal, and exposing a queue-style stdin-injection point for
   dialog-manager messages (same `QuickAsideCapability.QUEUE` semantics
   already used by the stream-json adapters).
2. **Proxy lifecycle manager** — starts a per-instance mitmproxy on a freshly
   allocated free port, scoped to the lifetime of the spawned child process;
   torn down when the child exits. Reuses the existing `proxy/server.py` /
   `proxy/session.py` machinery, just instantiated per `vc` invocation
   instead of globally via `serve`.
3. **Focus router** — OS-level foreground-window detection, matching the
   foreground window to either the spawned CLI's terminal or its browser
   panel. Tracks two pieces of state:
   - `current_focus`: the instance whose terminal or panel is the live
     foreground window right now, or `None` if the foreground window belongs
     to neither.
   - `last_vc_focus`: the instance whose terminal or panel was most recently
     the foreground window — updated only when focus *enters* a
     voice-copilot-related window (terminal or panel), and left unchanged
     while focus is on unrelated windows. Never `None` once any
     voice-copilot window has been focused at least once.

   Drives two independent behaviors (each may be toggled separately):
   - **Hotkey routing**: push-to-talk audio is always delivered to
     `current_focus` (if `None`, the hotkey has no target).
   - **Narrate-only-when-focused** (new global checkbox in
     `~/.voice-copilot/config.yaml`): selects which pointer gates TTS.
     - **Checked**: the narrating instance is `current_focus`. The moment
       focus leaves to *any* other window — including a single running
       instance losing focus to an unrelated app — that instance's
       commentator stops invoking TTS for new events; narration resumes the
       instant it regains real foreground focus. The text event log keeps
       updating in the panel regardless; only the voice stops.
     - **Unchecked**: the narrating instance is `last_vc_focus` — sticky.
       Narration keeps going for whichever instance you last looked at, even
       while you've switched away to an unrelated window (e.g. a text
       editor), and only changes when you focus a *different*
       voice-copilot-related window (another instance's terminal or panel).
       With a single running instance this is equivalent to "always
       narrate" once that instance has been focused once.

   In both modes exactly one instance narrates at a time, preventing
   multiple simultaneously-open `vc` sessions from talking over each other.
4. **Web panel additions** — per-instance URL (existing), a "Proxy settings"
   tab for Tier 2, an "Add a CLI" instructions view for Tier 3, and the new
   narrate-only-when-focused checkbox (reflects/edits the global config
   value; same value across all instances since it's a shared setting).

## Data flow (Tier 1 example)

`vc claude` → dispatcher resolves `claude` profile (verified) → allocate
panel port + proxy port → start this instance's FastAPI app → start proxy →
spawn `claude` in PTY with `ANTHROPIC_BASE_URL` merged into child env only →
open browser to panel → bridge PTY ⇄ real terminal → proxy intercepts API
traffic → structured `Event`s onto the bus → commentator → (if focused) TTS.
Push-to-talk while focused → STT → dialog manager → injected into PTY stdin
at next turn boundary.

## Error handling

Consistent with the project's "fail loud, no silent fallback" rule:

- Proxy port already in use → pick the next free port automatically; this is
  not a semantic failure, no user-facing message needed.
- Proxy fails to start for another reason (e.g. CA cert not trusted yet) →
  the target CLI still launches as a plain PTY (never block the user's
  actual work), but the panel shows an explicit error with remediation steps.
  Never silently narrate nothing while pretending to work.
- Tier 2 with incomplete settings → panel banner stays visible until
  resolved; no narration, no guessing at missing values.
- Alias install (`vc` shim) conflicts → skip silently once, never overwrite;
  this is the one case where silence is correct, since there's nothing wrong
  to report — the user's existing `vc` is simply left in charge.

## Testing

- Unit: profile resolution (name → tier) for verified / unverified / absent
  cases; env merge logic proven not to mutate `os.environ` of the parent
  process.
- Integration: a fixture CLI script spawned through the PTY manager +
  per-instance proxy, asserting structured events reach the bus.
- Integration: focus-router state changes correctly gate both hotkey
  delivery and TTS invocation (mock two instances, toggle simulated focus,
  assert only the focused one calls the TTS provider).
- Manual verification before calling this done: real `vc claude` and
  `vc codex` end to end against the live providers (per
  verification-before-completion) — automated tests alone don't confirm the
  actual CLIs honor the env override in practice.
