# Changelog

## Unreleased

### Added
- Plugins and hooks: twelve coding CLIs can report to Voice Copilot through
  their own plugin or hook system, without the proxy, and answer to pause,
  Supervisor stop and voice messages. Claude Code (HTTP hooks), Pi
  (extension), OpenCode (plugin) and Hermes Agent (Python plugin) were tested
  end to end; Codex, Gemini CLI, Qwen Code, Copilot CLI, Grok Build, Droid,
  OpenHands and Kimi get hook entries from their published references.
  `vc claude`, `vc pi` and Launch load the plugin for the session;
  `voice-copilot integrate <cli>` and the panel's new Plugins tab install it
  for good. See `docs/integrations.md`.
- `voice-copilot-hook`, the forwarder those hook entries run, and the
  `/api/companion/v1` endpoints behind them (RFC 0001 over HTTP).
- Narration says what the agent is waiting for ("permission to use Bash:
  rm -rf build") instead of just that it waits.

- Piper voices run on your computer (`tts.name: piper`, `local-tts` extra):
  sherpa-onnx instead of PyTorch, about 45 MB. Each narration language gets
  a default voice, downloaded once on first use (about 65 MB); after that
  speech output needs no network.

### Changed
- The `local-tts` extra installs sherpa-onnx instead of PyTorch (about 2 GB
  that no engine used).
- Narrator, summary and Supervisor prompts are English templates for every
  language, plus native example lines per language and a closing line that
  names the reply language. In blind A/B runs on Haiku 4.5 the new narrator
  won 79 of 99 comparisons against the old per-language prompts and stopped
  mixing languages (a Ukrainian line with Russian words in it).
- The panel's icon font ships with the package: no request to Google Fonts,
  icons work offline.

### Security
- The local panel refuses forged cross-site requests: it checks the `Host`
  header (DNS rebinding) and the `Origin` of state-changing requests and
  WebSocket handshakes. Before, any web page could launch a CLI, install PATH
  shims, read the live trace over `/ws`, or (via rebinding) send a stored API
  key to a base URL of its choice. The proxy refuses browser requests from
  other sites too.
- The project's `.env` no longer leaks into the wrapped agent's environment
  (an `ANTHROPIC_API_KEY` there switched Claude Code to API billing).
- The Claude Code narrator runs tool-free (`--disallowedTools "*"`); the
  Copilot CLI narrator can no longer run a shell or write files
  (`--deny-tool shell --deny-tool write` instead of `--allow-all`).
- The proxy listens on 127.0.0.1 whatever address the panel binds to
  (`VOICE_COPILOT_PROXY_HOST` changes that). Before, `VOICE_COPILOT_HOST=0.0.0.0`
  also put an open model relay on the network.
- A panel on a network address answers other devices only with its token:
  before, anyone on the network could launch a terminal, read the trace or
  replace the API keys. `serve` prints the link to open on a phone; the
  browser keeps the token in a cookie. Requests from this computer need no
  token, unless `VOICE_COPILOT_TOKEN` is set, which extends the check to every
  caller (the hooks and plugins send it).

### Fixed
- `vc` runs the agent in the current folder, not the one picked in the panel.
- Launching from the panel narrates through the launched CLI (the Quickstart's
  "no extra API key" now holds); before, it used the saved API provider.
- Every normal `vc` exit raised `PtyProcessError`; workers the CLI left
  behind are stopped with it. The PTY follows terminal resizes on
  macOS/Linux.
- A stream-JSON line over 64 KiB killed the Claude Code / Codex reader and
  froze the CLI.
- The proxy asked upstream for brotli it can't decode; a parser error could
  cut the user's API stream; tool rounds on Chat Completions and Ollama read
  as "turn ended".
- A user's own base URL (an Anthropic-compatible vendor, a gateway) stays the
  upstream under `vc` instead of sending that vendor's token to the default
  host.
- Supervisor: reviews even when the narrator fails; a STOP during a held line
  keeps the agent paused; its warnings are no longer replaced by the next
  narration line; es/fr/uk get it in their own language.
- Urgent events (edits, failures) are spoken at once instead of waiting for
  the idle timer; a line that fails in TTS releases the hold on the agent.
- On macOS/Linux, `narrate_only_when_focused` no longer mutes `vc` narration
  while the terminal has focus.
- `Alt+M` mutes, `serve --demo` works, `run codex --proxy` narrates,
  `vc claude --resume` passes the flag through, the tray's Quit quits,
  `VOICE_COPILOT_LOG=debug` no longer crashes.
- Settings refuse a provider that can't be built instead of saving it and
  failing every later start; the codex route migration runs once, so
  `openai` can be kept; config files are written atomically.
- Silero and Deepgram say they are not implemented yet instead of asking for
  an extra that does not help.
- Catalog: DeepSeek Harness launches `dsh web` on an Anthropic-format route;
  Pi and Grok link their current projects.
- One event that breaks the narrator or the TTS driver no longer silences the
  rest of the run.
- OpenAI reasoning models (o-series, GPT-5) can narrate: they get
  `max_completion_tokens`, no `temperature` and a low reasoning effort.
- Two copies of the same CLI behind one proxy are two sessions. Claude Code,
  Codex, OpenCode, Crush and Cline are told apart by the session id they
  send; before, the same User-Agent and key merged them. A plugin and the
  proxy report such a session under one id, so pause, stop and voice
  messages reach the terminal being narrated.
- The proxy reuses one pooled upstream connection instead of a new TLS
  handshake per model call, and gives up connecting after 15 seconds.
- Settings apply when saved: the voice, speech input and hotkeys used to wait
  for a restart, and so did the narrator's debounce. A voice that cannot start
  is refused like a commentator; one that failed at launch reports why on each
  line instead of staying silent.
- Ctrl+C while a line was being spoken left `serve` and `vc` hanging: the
  speech loop swallowed its own cancellation.

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
