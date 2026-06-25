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

This spec covers the new launch path only. It is additive to, and reuses
building blocks from, functionality that already ships today:

- The headless stream-json adapters (`adapters/claude_code.py`,
  `adapters/codex.py`, used by `voice-copilot run`) are unchanged — they
  remain the mechanism for headless/batch use (`-p`/`exec` style
  invocations), a different use case from "narrate my interactive session".
- `proxy/cli_catalog.py` + `proxy/cli_catalog.yaml` already curate 16 known
  CLIs (claude, codex, copilot, aider, opencode, kimi, gemini, qwen, crush,
  cursor, goose, amp, continue, openhands, auggie, grok) with their
  provider and override-env-var mapping.
- `proxy/cli_shims.py` already resolves a catalog entry's binary and
  computes its env overrides (`_resolve_binary_path`, `_proxy_env_overrides`,
  `base_urls_for`), and already offers two ways to use that: **install** a
  permanent PATH shim (so plain `claude` is proxied in every terminal,
  forever, until restored) and **launch** (opens a brand-new, fully
  detached terminal window with the override env set). Both are exposed
  today only as buttons in the web Settings panel.

What's still missing, and what this spec actually adds, is a third way to
use that same env-resolution logic: typed directly in a terminal
(`vc <name>` / `voice-copilot <name>`), scoped to *that one invocation*
(not a permanent PATH mutation), with the target CLI's terminal **and**
the ability to inject push-to-talk messages into it — something neither
install (no live connection back to voice-copilot) nor launch (detached,
unconnected window) nor headless `run` (no live terminal at all) provides
today. The new command reuses the catalog/env-resolution code from
`cli_shims.py` rather than re-implementing it.

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

## CLI profile resolution — reusing the existing catalog

No new registry/schema. `<name>` is resolved using exactly the lookup
`proxy/cli_shims.py` already has, in this order:

1. **`CLI_CATALOG[<name>]`** (from `cli_catalog.yaml`, keyed by `command`,
   e.g. `claude`, `codex`, `opencode`, ...) — the 16 curated entries.
2. **`cfg.proxy_cli.profiles[<name>]`** — today this dict only overrides
   fields (`binary_path`, `working_directory`, `provider`) for IDs that
   already exist in `CLI_CATALOG`; `_meta_for()` raises `KeyError` for any
   other key. This spec extends that: a `cfg.proxy_cli.profiles` entry for a
   name *not* in `CLI_CATALOG` is now also accepted, supplying `provider` +
   `base_url_env` directly (the two fields `_proxy_env_overrides` actually
   needs — `label`/`description`/`website_url` are catalog-only display
   metadata, not required to compute an env override or launch a process).

### Resolution per `vc <name>` invocation

1. **Known** — `<name>` resolves via either lookup above, and its binary
   resolves on PATH (or via a configured `binary_path` override, same
   resolution `_resolve_binary_path` already does). Allocate a proxy port,
   compute env overrides with the existing `_proxy_env_overrides` logic,
   spawn `<name>` in a PTY with that env merged into the *child's*
   environment only (never mutates voice-copilot's own `os.environ`), open
   the browser panel. Fully automatic — no new "verified" concept needed,
   since a catalog entry is trusted by curation and a user-added config
   entry is trusted by the user having written it themselves.
2. **Unknown** — `<name>` matches neither `CLI_CATALOG` nor
   `cfg.proxy_cli.profiles`. Spawn `<name>` as a plain PTY pass-through (no
   env override attempted), open the panel showing "this CLI isn't
   recognized" with a short example of adding a `proxy_cli.profiles.<name>`
   entry to `~/.voice-copilot/config.yaml` (`provider` + `base_url_env`).
   The CLI launches immediately; the instruction is informational, not a
   blocking prompt.

Binary-not-found (catalog/config entry exists but nothing resolves on PATH)
is not a third tier — it's a plain error, handled the same way
`install_cli_shim` already handles it today: surfaced in the panel with a
prompt to set a Binary override in Settings, same field that already exists
for the Install/Launch buttons.

This resolution applies only to the `vc <cli>` interactive path. It has no
bearing on the existing headless stream-json adapters, which remain
separate, hand-written per-CLI parsers for CLIs whose own stream-json wire
format we've chosen to support for batch use.

## Components

1. **PTY process manager** — cross-platform child process bridge
   (ConPTY on Windows) wiring the child's stdin/stdout/stderr to the user's
   real terminal, and exposing a queue-style stdin-injection point for
   dialog-manager messages (same `QuickAsideCapability.QUEUE` semantics
   already used by the stream-json adapters).
2. **Proxy lifecycle manager** — starts a per-instance proxy on a freshly
   allocated free port, scoped to the lifetime of the spawned child process;
   torn down when the child exits. Reuses `proxy/server.py`'s
   `build_proxy_server`/`base_urls_for` and `proxy/session.py`'s
   `SessionRegistry`, just instantiated per `vc` invocation instead of
   globally via `serve`/`proxy`. Resolution (binary path, env overrides) is
   the existing `proxy/cli_shims.py` logic, per the CLI profile resolution
   section above.
3. **Focus router** — OS-level foreground-window detection, matching the
   foreground window to either the spawned CLI's terminal or its browser
   panel. Conceptually it tracks two pieces of state:
   - `current_focus`: the instance whose terminal or panel is the live
     foreground window right now, or `None` if the foreground window belongs
     to neither.
   - `last_vc_focus`: the instance whose terminal or panel was most recently
     the foreground window — updated only when focus *enters* a
     voice-copilot-related window (terminal or panel), and left unchanged
     while focus is on unrelated windows. Never `None` once any
     voice-copilot window has been focused at least once.

   > **Implementation note (as shipped):** because each `vc` invocation is
   > its own process with its own router (there is no shared in-memory
   > router), this is realized per-process rather than as one object holding
   > both pointers. `FocusRouter.current_focus` is a plain `bool` — "is *this*
   > instance the foreground window" (terminal OR panel) — and the
   > cross-instance `last_vc_focus` arbitration lives in a small shared
   > `focus-state.json` in the config dir: `record_focus()` stamps this
   > instance's pid+timestamp when it gains focus, and `is_last_focused()`
   > reports whether this instance wrote the most recent stamp. This delivers
   > the same observable behavior (exactly one instance narrates; sticky when
   > unchecked) across separate processes, which a single `None`-valued
   > pointer could not.

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
4. **Web panel additions** — per-instance URL (existing), an "unrecognized
   CLI" instructions view for the Unknown case, a binary-not-found prompt
   reusing the existing Binary override field/copy from the Install/Launch
   flows, and a **Settings** tab holding the narrate-only-when-focused
   checkbox
   (reflects/edits the global config value; same value across all instances
   since it's a shared setting). Label and helper text for the checkbox:

   > **Narrate only when focused**
   > When checked, this instance only speaks while its terminal or this
   > panel is the active window — switching away mutes it instantly, even
   > if it's the only instance running. When unchecked, narration stays on
   > the last voice-copilot window you focused until you focus a different
   > one, so briefly switching to another app (e.g. to take notes) doesn't
   > cut off the voice.

## Data flow (known-CLI example)

`vc claude` → dispatcher resolves `claude` via `CLI_CATALOG` → allocate
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
- Binary not found on PATH (known CLI, but nothing to launch) → same
  remediation `install_cli_shim` already gives today: panel prompt to set a
  Binary override in Settings; no narration, no guessing at a path.
- Alias install (`vc` shim) conflicts → skip silently once, never overwrite;
  this is the one case where silence is correct, since there's nothing wrong
  to report — the user's existing `vc` is simply left in charge.

## Testing

- Unit: profile resolution (name → known via `CLI_CATALOG` / known via
  user-added `cfg.proxy_cli.profiles` entry / unknown) and the
  binary-not-found error path; env merge logic proven not to mutate
  `os.environ` of the parent process.
- Integration: a fixture CLI script spawned through the PTY manager +
  per-instance proxy, asserting structured events reach the bus.
- Integration: focus-router state changes correctly gate both hotkey
  delivery and TTS invocation (mock two instances, toggle simulated focus,
  assert only the focused one calls the TTS provider).
- Manual verification before calling this done: real `vc claude` and
  `vc codex` end to end against the live providers (per
  verification-before-completion) — automated tests alone don't confirm the
  actual CLIs honor the env override in practice.
