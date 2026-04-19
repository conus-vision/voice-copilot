# CLAUDE.md

Project: **voice-copilot** — open-source voice pair-programmer for LLM CLIs (Claude Code, Codex CLI, Gemini CLI, Aider, …).

## Product idea

The tool sits next to an LLM coding agent and narrates what the agent is doing — decisions taken, files changed, reasoning summarised — as a calm human-voice stream. The user can press a push-to-talk hotkey, ask a question or give a correction, and that message gets injected back into the running agent.

Think: a programmer on a screen-share, talking through what they're doing while their partner can interrupt at any time.

## High-level architecture

```
┌──────────────────────────────────────────────────────────┐
│ Browser (popup + settings)          ← localhost:PORT      │
│   getUserMedia → WebSocket        (mic in)                │
│   <audio> ← WebSocket             (TTS out)               │
└──────────────────────────────────────────────────────────┘
                     ▲  ▲
                     │  │   WebSocket (events, audio frames)
                     ▼  ▼
┌──────────────────────────────────────────────────────────┐
│ Python server  (FastAPI + uvicorn + asyncio)              │
│                                                           │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐  │
│  │ CLI adapter  │──▶│  Event bus   │◀──│ HTTP proxy   │  │
│  │ (stream-json │   │ (asyncio)    │   │ (mitmproxy)  │  │
│  │  subprocess) │   └──────┬───────┘   └──────────────┘  │
│  └──────┬───────┘          │                              │
│         │                  ▼                              │
│  stdin  │          ┌───────────────┐                      │
│  inject │          │ Commentator   │  Haiku/gpt-4o-mini/  │
│         │          │ (debounce,    │  local ollama        │
│         │          │  classify,    │                      │
│         │          │  summarize)   │                      │
│         │          └──────┬────────┘                      │
│         │                 ▼                               │
│         │          ┌───────────────┐                      │
│         │          │ TTS provider  │  edge-tts / Silero / │
│         │          │               │  ElevenLabs / OpenAI │
│         │          └───────────────┘                      │
│         ▲                                                 │
│  ┌──────┴───────┐  ┌───────────────┐  ┌───────────────┐  │
│  │ Dialog mgr   │◀─│ STT provider  │◀─│ Hotkey / Tray │  │
│  │ (queue,      │  │ (whisper-api/ │  │ (pynput/      │  │
│  │  /btw, inj.) │  │  f-whisper/   │  │  pystray)     │  │
│  │              │  │  deepgram)    │  │               │  │
│  └──────────────┘  └───────────────┘  └───────────────┘  │
└──────────────────────────────────────────────────────────┘
```

### Three interception strategies (A first-class, B first-class, C fallback)

- **A. Stream-JSON mode of the target CLI** — e.g. `claude -p --output-format stream-json`, `codex exec --json`. Structured events, reliable, but CLI runs headless (no TUI).
- **B. HTTP proxy** — mitmproxy sets on `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL`. Captures the raw SSE including `thinking` blocks. Works alongside interactive TUI.
- **C. PTY wrapper fallback** — wraps any binary, parses rendered stdout. Low-quality narration but guarantees coverage.

### Injecting voice into a running agent — per-adapter capability

Adapters declare a `QuickAsideCapability`:

1. **native** — CLI supports a built-in "side question" channel (e.g. Claude Code `/btw <q>`). Inject immediately.
2. **queue** — no native channel; we write the message to subprocess stdin at the next turn boundary (or on hard-interrupt).
3. **manual** — fully external CLI; we copy the question to the clipboard and show it in the popup so the user pastes it.

### Providers are pluggable

All of **TTS**, **STT**, **commentator LLM** implement a small interface and are selected via config. Defaults are cloud-light; local backends are optional extras.

| | Default (light) | Local (extra `[local]`) | Premium cloud |
|---|---|---|---|
| TTS | `edge-tts` | `silero`, `piper` | `elevenlabs`, `openai-tts` |
| STT | `openai-whisper-api` | `faster-whisper` | `deepgram` |
| LLM | `anthropic` (Haiku 4.5) | `openai-compat` (Ollama / LM Studio) | `openai` |

## Directory layout

```
voice_copilot/
  core/        event bus, event types, config
  adapters/    CLI adapters (claude_code, codex, proxy, pty_fallback)
  llm/         commentator LLM providers
  tts/         TTS providers
  stt/         STT providers
  commentator/ debounce + prompt assembly + i18n
  dialog/      queue + quick-aside + interrupt flow
  web/         FastAPI app + static popup/settings UI
  hotkeys.py   global hotkey listener (pynput)
  tray.py      system tray icon (pystray)
  cli.py       `voice-copilot run <target-cli> -- ...`
```

## Run model

`voice-copilot run claude -p "implement feature X"` does:

1. Start FastAPI on `127.0.0.1:<port>`, open browser to popup URL.
2. Spawn the tray icon and register global hotkey.
3. Optionally start mitmproxy and set `ANTHROPIC_BASE_URL` for the child.
4. Spawn the child CLI with appropriate JSON-stream flags.
5. Pipe events into the bus → commentator → TTS.
6. When user presses push-to-talk → record → STT → dialog manager decides: quick-aside / queue / manual.

## Conventions

- Python 3.11+. Formatter: **ruff format**. Linter: **ruff**. Type checker: **mypy** (strict on new code).
- Package manager: **uv** (`uv sync`, `uv run voice-copilot ...`). pipx also supported.
- Config lives at `~/.voice-copilot/config.yaml`; secrets in OS keyring or `.env`.
- Prompt files are markdown in `voice_copilot/commentator/prompts/<lang>.md` — treat them as product surface.
- No hidden retries, no silent fallbacks between providers: if configured provider fails, surface the error to the popup and stop narrating. Fail loud.

## Non-goals (at least for v1)

- We do **not** try to be a TUI replacement. The browser popup is the UI for voice-copilot itself; the underlying CLI's TUI (if any) stays as-is.
- We do **not** re-implement CLIs. If Claude Code doesn't expose some event, we accept that and narrate what we have.
- We do **not** train models.

## Memory for future Claude sessions

Persistent notes about this user and project live in
`C:\Users\vladi\.claude\projects\f---VOICE-COPILOT\memory\` (indexed by `MEMORY.md`).
Use them — don't re-derive the project's purpose from scratch each session.
