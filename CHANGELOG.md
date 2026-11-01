# Changelog

## 0.2.0 — 2026-11-01

Plugins and chat commands.

### Added
- **Plugins** for Claude Code, Codex, Pi, oh-my-pi and DeepSeek Harness: keep
  starting your CLI as usual, and the first session brings Voice Copilot along.
  `voice-copilot setup` connects whichever you have. See `docs/plugins.md`.
- **Commands in the chat:** type `vc` in any connected CLI for the list. `vc off`
  for this session, `vc off all` for every one, `vc check`, `vc ask`, `vc speed`,
  `vc volume`, `vc model`, `vc review` and more. Only a message that is exactly a
  command counts. CLIs without a full plugin get the same words through the proxy.
- **Step in from the panel:** on Claude Code your message reaches the agent as a
  real message in the middle of its turn. On other CLIs it arrives as your own
  message where the CLI allows it, else as hook text; `docs/plugins.md` has the table.
- A greeting when a session starts, so you know it's connected and what to type.

### Fixed
- The panel's Trace autoscroll did nothing: the whole page grew and scrolled instead
  of the list. The page now stays the size of the window.

## 0.1.0 — 2026-09-02

The Supervisor release.

### Added
- **Supervisor** — a second, stronger model reviews the agent at checkpoints
  (end of turn, every N tool calls, a failure that repeats) and warns you out
  loud; **Supervisor+** also pauses the agent on a hard stop, with a *Resume
  agent* banner in the panel. Per-CLI override, own model, live status in the
  launch banner. See `docs/supervisor.md`.
- **Automatic model tiers** — one checkbox picks the weakest model for the
  narrator and the strongest for the Supervisor from the CLI's own catalog
  (Codex) or static tiers (Claude Code).
- **Hold the agent while a line is read** — topbar toggle; the agent is
  suspended from the start of a narration until the browser reports it
  played, with a watchdog so a silent browser never wedges it.
- Codex on a ChatGPT plan is narrated: `openai_base_url` config flag instead of
  the ignored env var, the ChatGPT backend route, WebSocket refusal with HTTP
  fallback, zstd request bodies, Responses-API query sniffing.
- Live topbar: green dot while narration runs, Skip after the speed buttons.
- A `.env` next to where you run `voice-copilot` is loaded at startup; shell
  exports still win. `.env.example` lists the keys.
- Releases publish to PyPI from a `vX.Y.Z` tag push (trusted publishing) and
  land as a GitHub release with the sdist and wheel attached.

### Removed
- The `proxy` extra: the reverse proxy has been httpx-based for a long time and
  never imported mitmproxy, which dragged ~40 packages into `[all]`. Unused
  `websockets` and `pydantic-settings` dependencies, the broken Dockerfile, the
  three shell launchers around `uv run voice-copilot`, internal planning docs
  and the 8 MB demo video (the README links YouTube).

### Fixed
- Saving settings no longer swaps the running `auto` commentator for the saved
  API provider.
- Narration was silently discarded whenever the CLI re-sent the conversation
  or asked for a session title (both the TTS driver and the panel treated
  those as a new question).
- `turn.ended` now says whether the agent is really done; sub-agent and
  CLI-internal events are tagged and ignored by narrator and supervisor.
- `vc` exits when the wrapped CLI does: its orphaned sub-agents are killed and
  servers stop waiting on their connections.
- Windows: ConPTY's win32-input-mode / focus-reporting requests no longer leak
  into the console (typed text came out as `^[[68;32;1074;1;0;1_`).
- Codex narration profile: prompt over stdin (the `.cmd` layer mangled it),
  a ChatGPT-plan model, `--ignore-user-config --ephemeral`.
- Blocked autoplay is now visible in the panel and recovers on the first click.
- Panel: every proxy route is selectable (a missing one broke every save).
- Headless Linux (no `DISPLAY`): importing the CLI no longer dies inside pynput;
  global hotkeys are reported as unavailable and everything else runs.
- Test suite: no longer hangs on Python 3.12 (`Server.wait_closed()` semantics)
  and passes on macOS; `pytest-timeout` turns any future hang into a failure.
