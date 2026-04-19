# voice-copilot

Voice pair-programmer for LLM coding CLIs. Narrates what the agent is doing, lets
you ask questions and push corrections by voice.

> **Status:** early scaffold. All 13 construction stages landed; smoke-tested end
> to end, but real-world vendor CLIs (Claude Code, Codex) still need rough-edge
> polish. Contributions welcome.

## What it does

- Wraps an LLM coding CLI (Claude Code, Codex CLI — more to come) and listens
  to its event stream in real time.
- A small **commentator LLM** (Haiku 4.5 by default) summarises decisions,
  file edits and reasoning in short human-voice lines.
- A **browser popup** on localhost exposes Play/Pause/Mute/Speak/Interrupt
  buttons and settings.
- **Push-to-talk** (default `Alt+Space`): your question goes to STT → then
  into the running agent as a native side-question, a queued next message,
  or to the clipboard (depending on CLI capability).
- **Pause the CLI** while you talk (`Alt+P` or auto-on-speak): the subprocess
  is suspended via `psutil`, no races with the agent.
- Works in English, Spanish, French, Ukrainian and Russian.
- Plug-in providers for TTS, STT and commentator LLM — run fully local or use
  cloud APIs.

## Install

```bash
# light default: cloud STT/TTS
pipx install voice-copilot

# + local TTS (Silero / Piper)
pipx install "voice-copilot[local-tts]"

# + local STT (faster-whisper)
pipx install "voice-copilot[local-stt]"

# everything local
pipx install "voice-copilot[all]"
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install voice-copilot
uvx voice-copilot run claude -- -p "refactor the auth module"
```

## Quick start

```bash
# 1. point voice-copilot at your Anthropic key (keychain-backed)
voice-copilot serve                 # opens settings → paste ANTHROPIC_API_KEY

# 2. wrap Claude Code
voice-copilot run claude -p "fix failing tests"

# 3. or wrap Codex CLI
voice-copilot run codex  -p "explain what this repo does"
```

A browser tab opens at `http://127.0.0.1:8765`. Hold `Alt+Space` to speak.

## Hotkeys

| Action                       | Default combo     | Notes                                        |
| ---                          | ---               | ---                                          |
| Push-to-talk                 | `Alt+Space`       | Hold to record, release to send to STT.      |
| Interrupt (pause & listen)   | `Alt+Shift+Space` | Suspends the CLI process.                    |
| Pause / resume toggle        | `Alt+P`           | Manual pause of the child CLI.               |
| Mute TTS                     | `Alt+M`           | Stops narration without affecting the agent. |

All four are rebindable on the settings page.

## Providers

Every layer is pluggable. Defaults are cloud-light so `pipx install voice-copilot`
works out of the box.

|          | Default (light)       | Local (extra)              | Premium cloud        | Secret name              |
| ---      | ---                   | ---                        | ---                  | ---                      |
| **TTS**  | `edge-tts`            | `silero`, `piper`          | `elevenlabs`, `openai` | `ELEVENLABS_API_KEY`, `OPENAI_API_KEY` |
| **STT**  | `openai-whisper-api`  | `faster-whisper`           | `deepgram`           | `OPENAI_API_KEY`, `DEEPGRAM_API_KEY` |
| **LLM**  | `anthropic` (Haiku)   | `openai-compat` (Ollama)   | `openai`             | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_COMPAT_API_KEY` |

Switch via the **Settings** page or by editing `~/.voice-copilot/config.yaml`.

## Configuration

- `~/.voice-copilot/config.yaml` — edited by hand or via the settings page.
- Secrets live in the **OS keychain** (Credential Manager / Keychain / Secret
  Service) or in a `.env` next to where you run `voice-copilot`.
- No fallbacks between providers: if the configured one fails, the error
  surfaces in the popup and narration stops. Fail loud, not silently.

## Interception strategies

1. **Stream-JSON mode** of the target CLI (Claude Code, Codex) — default when
   available.
2. **HTTP reverse-proxy** (`--proxy`) — routes `ANTHROPIC_BASE_URL` / 
   `OPENAI_BASE_URL` through us so we can narrate `thinking` blocks even when
   the underlying TUI hides them. No CA certs needed.
3. **PTY fallback** — wraps any binary. Lower fidelity, last resort.

See [docs/architecture.md](docs/architecture.md).

## Development

```bash
git clone https://github.com/voice-copilot/voice-copilot
cd voice-copilot
uv sync --extra dev
uv run voice-copilot serve --demo     # emit synthetic events, exercise the UI
uv run ruff check .
uv run mypy voice_copilot
uv run pytest
```

## Troubleshooting

- **No voice output** — open DevTools in the popup, check the audio element
  is receiving `audio_header`/bytes frames. Most browsers need a user click
  before autoplay unlocks; click anywhere in the popup once.
- **Mic denied** — the popup only works over `http://127.0.0.1:<port>` (a
  localhost-trusted origin). Don't serve it from a LAN IP without HTTPS.
- **`keyring` says no backend** on headless Linux — `pip install keyrings.alt`
  or set env vars instead.
- **Commentator silent** — check `commentator.min_importance` on the settings
  page; set to `low` to hear everything while debugging.

## License

MIT — see [LICENSE](LICENSE).
