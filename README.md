# Voice Copilot

Voice Copilot is a listening-first companion for LLM coding CLIs. It uses a
separate commentator LLM to analyze what the coding agent is doing, summarize
the important parts of its reasoning and response, and let you intervene at the
right moment.

Instead of constantly reading terminal output, you can listen to short spoken
updates, glance at the trace when needed, and step in by voice when the agent
needs correction. The main value is lower cognitive load and better situational
awareness while the coding agent works.

> **Status:** 0.0.1 alpha.
> This is the first public alpha release.
> Created by Volodymyr Moskvin, Conus Vision.
> Contact: info@conus.vision | https://conus.vision
> We are open to collaboration and to growing the project together.

## Project direction

- The Voice Copilot core is open-source under MIT to maximize adoption,
  experimentation, and community contributions.
- Launch messaging drafts for video and social posts live in
  [docs/launch/README.md](docs/launch/README.md).
- The CLI companion integration RFC lives in
  [docs/cli-companion-interface.md](docs/cli-companion-interface.md), with the
  normative schema in
  [docs/schemas/cli-companion-interface.schema.json](docs/schemas/cli-companion-interface.schema.json).

## Alpha status and development plans

Voice Copilot is currently in its first alpha release.

The current goal is to validate the core idea with advanced users: a separate
commentator LLM that helps you follow what a coding agent is doing by listening
first, reading when needed, and intervening at the right moment.

Planned areas of development:

- improve narration quality, timing, and signal-to-noise ratio
- stabilize multi-session workflows and session switching
- expand structured integrations with more coding CLIs
- refine the companion interface together with CLI authors so it matches real
  integration needs
- improve advanced configuration, onboarding, and developer documentation
- explore richer host UIs such as VS Code while keeping the core workflow
  lightweight

## What Voice Copilot Is Not

- Not a prompt dictation app or voice keyboard
- Not a replacement for Claude Code, Codex, Gemini CLI, or other coding agents
- Not just text-to-speech for raw terminal logs
- Not direct verbatim reading of the model's hidden thinking or full answer
- Not another chat UI you need to stare at all day

## How Narration Works

Voice Copilot turns a coding agent's event stream into short spoken updates.

A separate lightweight commentator model watches the coding agent's
event stream and produces short summaries of what the agent appears to be doing,
why it is doing it, and what changed. In other words, this is parallel analysis
and summarization, not direct playback of the coding model's thinking or answer.

That is what makes the listening experience useful: you hear compressed,
actionable updates instead of a long stream of raw text, which reduces
cognitive load while preserving the option to inspect the trace and intervene.

## What it does

- Wraps an LLM coding CLI (Claude Code, Codex CLI — more to come) and listens
  to its event stream in real time.
- A small **commentator LLM** (Haiku 4.5 by default) summarises decisions,
  file edits and reasoning in short human-voice lines.
- Keeps listening as the primary experience: hear what matters, read the trace
  when useful, and interrupt only when needed.
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

## Narrate _any_ CLI via proxy mode

`voice-copilot run <target>` only knows `claude` and `codex`. For everything
else (aider, opencode, Cline, GitHub Copilot CLI that hits OpenAI/Anthropic),
run the proxy as a standalone service and point your CLI's `BASE_URL` at it:

```bash
voice-copilot proxy
# → prints ANTHROPIC_BASE_URL=http://127.0.0.1:8766/anthropic
#          OPENAI_BASE_URL   =http://127.0.0.1:8766/openai/v1
#          ...and OpenRouter / Groq / Mistral / Ollama / Gemini

# in another terminal:
ANTHROPIC_BASE_URL=http://127.0.0.1:8766/anthropic \
  aider --model anthropic/claude-3-5-sonnet-20241022
```

The popup shows one entry per connected client (seen via distinct
`Authorization` + `User-Agent`). Pick from the dropdown in the header to
choose which one to narrate — the others keep running silently.

Supported upstream providers:

| Provider   | Env var                  | Upstream                                   |
| ---        | ---                      | ---                                        |
| Anthropic  | `ANTHROPIC_BASE_URL`     | `api.anthropic.com`                        |
| OpenAI     | `OPENAI_BASE_URL`        | `api.openai.com`                           |
| OpenRouter | `OPENROUTER_BASE_URL`    | `openrouter.ai/api`                        |
| Groq       | `GROQ_BASE_URL`          | `api.groq.com/openai`                      |
| Mistral    | `MISTRAL_BASE_URL`       | `api.mistral.ai`                           |
| Ollama     | `OLLAMA_BASE_URL`        | `127.0.0.1:11434` (local)                  |
| Gemini     | `GEMINI_BASE_URL`        | `generativelanguage.googleapis.com` (passthrough) |

OAuth-authenticated CLIs (Claude Code subscription, Codex login flow) work
out of the box — we only see the bearer token on the wire and forward it.
The OAuth browser round-trip happens on different domains that we don't
intercept.

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
   available. Use `voice-copilot run claude` / `run codex`.
2. **HTTP reverse-proxy** (`voice-copilot proxy`, or `run … --proxy`) —
   routes provider `BASE_URL`s through us so we can narrate `thinking`
   blocks even when the underlying TUI hides them. Works with any CLI that
   respects `*_BASE_URL` env vars. No CA certs, no TLS interception — the
   client just talks plain HTTP to localhost. When `--proxy` runs alongside
   a stream-JSON adapter, the adapter suppresses duplicate LLM events so
   the proxy is the single source of truth.
3. **PTY fallback** — wraps any binary. Lower fidelity, last resort.

See [docs/architecture.md](docs/architecture.md).

## Development

```bash
git clone https://github.com/conus-vision/voice-copilot
cd voice-copilot
uv sync --extra dev
uv run voice-copilot serve --demo     # emit synthetic events, exercise the UI
uv run ruff check .
uv run mypy src/voice_copilot
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

## Contact

For collaboration, feedback, integrations, and community discussions:

- Email: info@conus.vision
- Website: https://conus.vision

## License

This repository is open-source under the MIT license.

That means individuals, teams, companies, and other open-source projects can
use, modify, fork, and redistribute the core with minimal friction.

See [LICENSE](LICENSE) and [LICENSING.md](LICENSING.md).
