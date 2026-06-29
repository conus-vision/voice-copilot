# Commentator `auto` default — reuse the launched CLI for narration

Status: approved design, not yet implemented.

## Goal

Make narration work **zero-config** when a CLI is launched via `vc`. Today
the commentator defaults to `provider: anthropic`, which fails for anyone
without an `ANTHROPIC_API_KEY` (it raised `TypeError: Could not resolve
authentication method`). Instead, by default the commentator should reuse
the **same CLI the user launched** to generate narration — using that CLI's
own auth (API key or subscription), requiring no extra keys or login. The
user can later switch to a dedicated provider.

## Background — what already exists

- The commentator builds an `LLMProvider` from `cfg.commentator.provider`
  (`commentator/pipeline.py` `_build_llm`). Providers implement
  `stream_chat()` and declare `prompt_style` of `"api"` or `"cli"`.
- `providers/llm/copilot_cli.py` already proves the approach: it shells out
  to the `copilot` binary via **stdin** (interactive mode, not `-p`, which
  triggers code-agent behaviour), with `prompt_style="cli"`, and its
  docstring notes *"No token extraction required — the CLI manages its own
  auth."* It has `_build_prompt`, `_make_cmd`, `_run_via_stdin`.
- `commentator/format.py` already produces a flat (non-bracket-labelled)
  prompt for `prompt_style="cli"`.

This feature **generalizes that pattern** to the launched CLI, and adds a
config model (global toggle + per-CLI overrides) and an always-on status
indicator in the panel.

## Non-goals

- No reuse of captured OAuth/API auth headers from the proxy (approach "A").
  We use each CLI's own headless invocation, which is ToS-clean (the CLI is
  used as intended) and needs no token handling.
- No attempt to make `auto` work outside a `vc` launch (standalone
  `serve`/`proxy`): there is no launched CLI to reuse, so `auto` surfaces a
  panel notice telling the user to pick a provider.
- No change to the existing API providers (anthropic/openai/openai-compat/
  copilot-cli/github-copilot) beyond extracting a shared subprocess runner.

## Config model

`CommentatorConfig` gains a global mode toggle and optional per-CLI
overrides. Existing fields (`debounce_ms`, `min_importance`, `speak_*`) are
unchanged. `provider` stays as the API provider used when mode/override is
`api`.

```yaml
commentator:
  mode: auto                      # global: "auto" (use launched CLI) | "api"
  provider:                       # the API provider; used when effective = api
    name: anthropic
    options: { model: claude-haiku-4-5-20251001 }
  per_cli:                        # optional overrides, keyed by CLI profile_id
    gemini: { mode: api, model: gemini-2.0-flash }
  debounce_ms: 1200
  min_importance: normal
  speak_tool_calls: true
  speak_thinking: true
  speak_file_edits: true
```

New pydantic types:

```python
class CommentatorCliOverride(BaseModel):
    mode: Literal["current", "api"]   # "current" = shell out to the launched CLI
    model: str | None = None

class CommentatorConfig(BaseModel):
    mode: Literal["auto", "api"] = "auto"
    provider: ProviderConfig = ProviderConfig(
        name="anthropic", options={"model": "claude-haiku-4-5-20251001"}
    )
    per_cli: dict[str, CommentatorCliOverride] = Field(default_factory=dict)
    # …existing fields unchanged
```

**Default change:** `mode` defaults to `auto`. `provider` keeps its existing
default so that selecting `api` (globally or per-CLI) still has a sane
provider to fall back on. Users with an explicit setup are unaffected; the
zero-config win is for the default path.

### Effective-provider resolution

For a `vc <cli>` launch (where `cli` is the resolved `profile_id`):

1. If `per_cli[cli]` exists → its `mode` wins (`current` or `api`), with its
   optional `model`.
2. Else the global `mode` applies (`auto` → `current`, `api` → `api`).
3. `current` → build `AutoCommentatorProvider(cli, binary, model=<override
   or the profile's cheap default>)`.
4. `api` → build the configured `provider` (unchanged behaviour).

## Components

1. **Narration-profile table** — `commentator/cli_profiles.py`: maps each
   supported CLI `profile_id` → how to invoke it for a *plain, cheap,
   tool-free* one-shot completion. Fields: `command` template / arg builder,
   `input_mode` (`stdin` | `arg`), default cheap `model`, and any
   tool/agent-disabling flags. Starting points (empirically tuned during
   implementation against the real CLIs):
   - `copilot` → reuse the existing copilot-cli invocation (stdin,
     `-s --allow-all --no-auto-update`, `gpt-5-mini`).
   - `claude` → `claude -p --model claude-haiku-4-5-20251001 --allowedTools ""`
     (cheap model + no tools so it narrates rather than acts), prompt via
     arg or stdin.
   - `codex` → `codex exec --model <cheap>` one-shot.
   - `opencode` → `opencode run --model <cheap>`.
   - `gemini` → `gemini -p -m <flash>`.
   A profile whose invocation can't be made to narrate cleanly is dropped
   from the table (so `auto` falls back to the panel notice for that CLI)
   rather than shipping broken.

2. **`AutoCommentatorProvider`** — `providers/llm/auto.py`,
   `@register("llm", "auto")`, `prompt_style="cli"`,
   `__init__(cli=None, binary=None, model=None)`. `stream_chat` looks up the
   profile for `cli`, builds the flat prompt (reusing copilot-cli's
   `_build_prompt`), runs the CLI via the shared runner, and yields the
   output text. If `cli` is unset or has no profile → raise a clear
   `RuntimeError` the commentator surfaces (status indicator + `ERROR`).

3. **Shared subprocess runner** — extract copilot-cli's `_run_via_stdin`
   into `providers/llm/_cli_runner.py` (stdin- or arg-fed prompt, timeout,
   returns `(stdout, stderr)`); `copilot_cli.py` and `auto.py` both use it.

4. **`vc` wiring** — in `_run_vc`, after resolving the launched CLI, compute
   the effective commentator provider (resolution above). When effective is
   `current`, construct the commentator with `AutoCommentatorProvider(cli=
   profile_id, binary=resolved_binary, model=…)`; when `api`, the existing
   path. Set the status indicator (below) either way. Unknown/unresolved CLI
   with mode requiring `current` → `auto` with no `cli` → fail loud.

5. **Status indicator** — always show which LLM the commentator uses this
   session, in the panel, with a link to the Commentator tab. Reuse the
   `app.state.launch_notice` → `/api/info` → panel-banner channel already
   built for `vc`, generalised to carry the commentator status string, e.g.
   *"Commentator: Claude (current CLI, haiku) — change in Commentator tab"*
   or *"Commentator: OpenAI gpt-5-mini (API)"*.

6. **Settings UI** (`web/static/`, Commentator tab) — a global mode toggle
   (Auto / API); when API, the existing provider+model fields; and an
   optional per-CLI overrides table (CLI → Current/API + model). The
   existing dotted-name config walker handles `mode` and `provider.*`; the
   `per_cli` table needs small JS to render/edit the map.

## Data flow (`vc claude`, default auto)

`vc claude` → `_run_vc` resolves `claude` → effective commentator =
`current` → `AutoCommentatorProvider(cli="claude", binary=…)` → per narration
batch, the commentator builds a flat prompt and the provider runs
`claude -p --model haiku --allowedTools "" "<prompt>"` (Claude's own auth) →
stdout → yielded as the narration → `COMMENTATOR_UTTERANCE` → Trace + TTS.
Status banner shows "Commentator: Claude (current CLI, haiku)".

## Error handling — fail loud

- `auto` with no resolvable CLI (not via `vc`, or unsupported CLI) → raises;
  the commentator publishes `ERROR` and the status banner reads "auto
  commentator needs a vc-launched supported CLI — pick a provider in the
  Commentator tab."
- Subprocess failure / timeout / empty output → the commentator's existing
  `try/except` in `_narrate` publishes `ERROR` (renders as an `ERROR` row in
  the Trace) and logs to the session log file. No silent no-op.
- Rate-limit / quota errors from the CLI surface the same way (visible),
  rather than vanishing.

## Caveats (documented, accepted)

- **Rate-limit contention:** `current` mode shares the launched CLI's
  account/quota with the agent itself, so on a throttled account narration
  competes with the agent (the user already sees `429` on Claude Pro). This
  is why `auto` is the *get-started* default, switchable to a dedicated
  provider for heavy use.
- **Latency:** a subprocess per narration batch is slower than a streaming
  API call (seconds, especially `claude -p` which starts the full agent).
  Acceptable for the default experience; a dedicated API/local provider is
  faster.
- **Empirical per-CLI tuning:** the exact flags that make each CLI produce a
  plain, cheap, tool-free completion are verified against the real binaries
  during implementation; unverified CLIs fall back to the notice.

## Testing

- **Unit:** profile lookup (cli → command/flags/model); `AutoCommentator
  Provider` with unset/unknown `cli` raises; the shared `_cli_runner` with a
  fake subprocess (stdin and arg modes, timeout); effective-provider
  resolution (global mode vs `per_cli` override); `vc` wiring constructs
  `auto` with the right `cli`/`binary` only when effective = `current`.
- **Config:** `mode` + `per_cli` round-trip through save/load; default
  `mode` is `auto`.
- **Manual (empirical gate, user-assisted):** for each of claude / codex /
  opencode / gemini / copilot, `vc <cli>` produces narration in the Trace
  that is cheap, reasonably fast, and tool-free (narrates, does not act).
  Any CLI that fails this is dropped from the profile table for v1.
