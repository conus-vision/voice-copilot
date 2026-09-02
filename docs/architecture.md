# Architecture

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
│  │ (stream-json │   │ (asyncio)    │   │ (reverse)    │  │
│  │  subprocess) │   └──────┬───────┘   └──────────────┘  │
│  └──────┬───────┘          │                              │
│         │                  ▼                              │
│  stdin  │          ┌───────────────┐                      │
│  inject │          │ Commentator   │  Haiku/gpt-4o-mini/  │
│         │          │ (debounce +   │  local ollama        │
│         │          │  LLM stream)  │                      │
│         │          └──────┬────────┘                      │
│         │                 ▼                               │
│         │          ┌───────────────┐                      │
│         │          │ TTS driver    │  edge-tts / Silero / │
│         │          │ (frames hub)  │  ElevenLabs / OpenAI │
│         │          └───────────────┘                      │
│         ▲                                                 │
│  ┌──────┴───────┐  ┌───────────────┐  ┌───────────────┐  │
│  │ Dialog mgr   │◀─│ STT           │◀─│ Hotkey / Tray │  │
│  │ (pause,      │  │ (whisper-api/ │  │ (pynput/      │  │
│  │  inject,     │  │  f-whisper/   │  │  pystray)     │  │
│  │  buffer)     │  │  deepgram)    │  │               │  │
│  └──────────────┘  └───────────────┘  └───────────────┘  │
└──────────────────────────────────────────────────────────┘
```

## Event bus

Single `asyncio.Queue` fan-out with drop-oldest back-pressure. Every component
(adapter, commentator, dialog manager, TTS driver, web WS) publishes and/or
subscribes via `EventBus.subscribe()`.

## Event kinds (selection)

| Kind                   | Who emits              | Who consumes          |
| ---                    | ---                    | ---                   |
| `AGENT_TEXT`           | CLI adapter, proxy     | Commentator           |
| `AGENT_THINKING`       | proxy (SSE thinking)   | Commentator           |
| `TOOL_CALL_STARTED/FINISHED` | CLI adapter      | Commentator           |
| `FILE_EDITED`          | CLI adapter            | Commentator           |
| `COMMENTATOR_UTTERANCE`| Commentator, Supervisor | TTS driver, UI       |
| `SUPERVISOR_VERDICT`   | Supervisor             | UI (Trace)            |
| `SUPERVISOR_STOP`      | Supervisor (`guard`)   | Dialog manager (pauses the CLI) |
| `USER_MESSAGE`         | STT, web, dialog       | Dialog manager        |
| `USER_SPEAK_REQUESTED` | Hotkey, web            | Dialog manager, TTS driver (barge-in) |
| `USER_INTERRUPT`       | Hotkey                 | Dialog manager        |
| `USER_PAUSE_TOGGLE`    | Hotkey                 | Dialog manager        |
| `AGENT_PAUSED/RESUMED` | Dialog manager         | UI                    |
| `TTS_STARTED/FINISHED` | TTS driver             | UI                    |
| `ERROR`                | any                    | UI                    |

## Pause / resume semantics

The CLI subprocess is suspended via `psutil.Process.suspend()` (works on
Windows / macOS / Linux without signals). Two paths:

- **Manual** — `Alt+P` (or the Pause button) toggles; dialog manager emits
  `AGENT_PAUSED`/`AGENT_RESUMED`.
- **Auto on speak** — if `dialog.auto_pause_on_speak` is on, the subprocess is
  paused while push-to-talk is held and resumed on release.

Off by default because pausing mid-tool-call is safe (threads just freeze) but
can be surprising for long-running commands.

## Injection — per-adapter

Adapters declare one of three `QuickAsideCapability` values:

- **native** — vendor CLI has a built-in side-question channel (e.g. Claude
  Code `/btw <q>`). Inject mid-turn.
- **queue** — no native channel, but stdin stays open — message lands on the
  next turn boundary.
- **manual** — fully external — copy to clipboard + show in popup.

## Audio framing

TTS output is sent over WebSocket as: a JSON `audio_header` frame → one or
more binary chunks → `audio_end`. The server holds a `broadcast_lock` during
each utterance so frames from different utterances never interleave.
Client-side, the browser wraps the bytes in a Blob URL for the `<audio>`
element and plays the queue sequentially. Barge-in (user starts speaking)
sends an `audio_interrupt` and cancels the in-flight synthesis.

## Interception strategies

1. **Stream-JSON** — Claude Code `-p --output-format stream-json
   --input-format stream-json --verbose`, Codex `exec --json`.
2. **HTTP reverse-proxy** — we start a tiny httpx-backed proxy on a local port
   and set `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` in the child env. Raw SSE
   including `thinking` blocks flows through us.
3. **PTY fallback** — wraps any binary, parses rendered stdout. Lower
   fidelity, planned for v0.1.


## Supervisor

The commentator pipeline runs two models with opposite budgets. The **narrator**
(cheap) turns every debounced batch of events into one or two spoken sentences.
The **supervisor** (the strongest model the launched CLI can run) is invoked
only at checkpoints — the end of the user's turn, a run of N tool calls, or the
same tool failing twice — and sees the goal, the rolling summary and the last
~80 formatted events. It answers `OK` / `WARN` / `STOP`; a warning is spoken,
and in `guard` mode a `STOP` publishes `SUPERVISOR_STOP`, which the dialog
manager turns into a pause of the wrapped CLI until the user resumes it.

Two proxy-side tags keep the checkpoints honest: events from a CLI's own side
requests (title generation, quota probes) carry `internal: true` and never
reach either model, and events from forked sub-agents carry `subagent: true`,
so a helper finishing is not mistaken for the task finishing. `TURN_ENDED`
carries `final: false` when the response ended in tool calls. See
[supervisor.md](supervisor.md).
