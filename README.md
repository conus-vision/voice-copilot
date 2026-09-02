<p align="center">
  <img src="https://raw.githubusercontent.com/conus-vision/voice-copilot/main/public/logo.png" width="440" alt="Voice Copilot">
</p>

> **Listen to Control** — a cheap model narrates your coding agent, a strong one supervises it, and you step in the moment it matters.

Coding agents now run for minutes at a time: reading files, editing code,
calling tools. You either babysit the terminal the whole time, or you look away
and miss the moment it goes in the wrong direction. **Voice Copilot narrates
what the agent is doing in short spoken updates, and a Supervisor — the
strongest model your CLI can run — reviews the work at checkpoints, warns you
when the agent drifts, and can pause it until you weigh in.** You keep
control without staring at the output.

<p align="center">
  <a href="https://www.youtube.com/watch?v=CFDFU5S1Grk">
    <img src="https://raw.githubusercontent.com/conus-vision/voice-copilot/main/public/poster.jpg" alt="Watch the Voice Copilot demo (60s)" width="100%">
  </a>
</p>

<p align="center">
  <a href="https://www.youtube.com/watch?v=CFDFU5S1Grk"><b>▶ Watch the 60-second demo</b></a>
  ·
  <a href="https://voice-copilot.conus.vision/">Website</a>
  ·
  <a href="#quickstart">Quickstart</a>
</p>

## Why this exists

Autonomous coding agents changed the loop. A single prompt can trigger minutes
of work, and the useful signal — what it understood, what it changed, what risk
it noticed — is buried in a fast-scrolling wall of text. Reading every line
defeats the point of delegating; ignoring the terminal means you only find out
about a bad turn after it happened.

Existing tools do not close this gap. Plain text-to-speech reads logs verbatim,
which is noise, not signal. Voice-coding tools dictate prompts *into* the agent
but tell you nothing about what it is *doing*. Voice Copilot sits in the
opposite seat: it is a **listening-first companion** that watches the agent on
your behalf, compresses the stream into short updates you can follow with your
eyes off the screen, and keeps a path to interrupt by voice when you need to
steer.

## How it works

Two models watch the agent's event stream, with opposite budgets:

- A cheap **narrator** (the weakest model your CLI offers, or Haiku 4.5 via
  API) speaks every few seconds: what the agent is doing right now, in one or
  two sentences. Parallel summarization — **not** verbatim playback of the
  model's hidden thinking.
- A strong **Supervisor** (the most capable model your CLI can run) looks
  only at checkpoints — the end of a turn, a run of tool calls, a failure
  that repeats — with the goal, a running summary and the recent transcript
  in front of it. It says `OK`, or it tells you what is wrong. In
  **Supervisor+** mode it also pauses the agent and waits for you.

```
 coding agent  ──►  event stream  ──►  narrator (cheap)     ──►  spoken update        ──►  you
 (Claude/Codex)     (json / proxy)     supervisor (strong)  ──►  warning · agent paused     (listen · resume · interrupt)
```

Both reuse the CLI you launched — its login, its models, no extra keys. The
result is a small fraction of one agent turn spent on oversight, and you hear
about a bad turn while it can still be stopped. Details in
[docs/supervisor.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/supervisor.md).

<a id="quickstart"></a>

## Quickstart (~3 minutes to first narration)

> Goal: hear Voice Copilot narrate a real Claude Code session. You need Python
> 3.11+ and an Anthropic API key.

**1. Install** (cloud-light defaults, no local models required):

```bash
pipx install voice-copilot          # or: uv tool install voice-copilot
```

**2. Add your key.** This opens settings in the browser — paste your
`ANTHROPIC_API_KEY` (stored in your OS keychain, not in a file):

```bash
voice-copilot serve
```

*Expected:* a tab opens at `http://127.0.0.1:8765` on the **Launch** tab, with
every coding CLI found on your machine listed.

**3. Launch an agent from the panel.** Set the working folder, then press
**Launch** next to Claude Code (or Codex, OpenCode, Droid, Cline, Copilot CLI,
…). It opens in a new terminal already routed through the local proxy — no
env vars to copy. The same thing from your own shell: `vc claude`.

*Expected:* the panel shows a live trace, and within a few seconds you **hear**
a short spoken summary of what the agent is doing. Click once in the panel if
the browser blocks autoplay.

That's the loop: **launch → listen → read the trace when you want detail.**

> **Voice input is temporarily disabled** in this build while the push-to-talk
> → STT → inject flow is reworked. Narration, the launcher and the trace are
> unaffected. Re-enable it with `voice_input: {enabled: true}` in
> `~/.voice-copilot/config.yaml`.

## Who it's for

**Vibe coders** building by feel, with the agent doing most of the typing.
Reading a fast wall of diffs and tool calls breaks the flow and the fun. Voice
Copilot keeps you in the creative loop: you hear what the agent decided and
changed in plain language, stay aware of the direction, and jump in by voice
the moment it drifts — no need to parse the terminal to stay in control.

**Professional engineers** running long, autonomous agent sessions (Claude
Code, Codex) on real codebases. The risk isn't typing speed, it's a confident
wrong turn buried in minutes of output. Voice Copilot surfaces the signal —
root cause, risk, next step — so you keep situational awareness while doing
something else, and interrupt early instead of reviewing a large bad diff after
the fact. Listening also lets you supervise more than one session without
staring at every token.

**Multitaskers and reviewers** who delegate work and need to know *when* to
step in, not read everything. Narration is the ambient channel: glance at the
trace only when an update tells you it matters.

**CLI authors** who want their tool to expose a clean event stream for
companion narration (see the integration RFC below).

> **Status: 0.1.0 alpha — the Supervisor release.** Aimed at advanced users
> comfortable testing CLI workflows and sharing feedback. Created by Volodymyr
> Moskvin, [Conus Vision](https://conus.vision). We are open to collaboration —
> [info@conus.vision](mailto:info@conus.vision).

## What it does

- Wraps an LLM coding CLI (Claude Code, Codex CLI — more to come) and listens
  to its event stream in real time.
- A **Supervisor** — the strongest model your CLI can run — reviews the agent
  at checkpoints and warns you out loud when it drifts, loops, touches files
  outside the task or runs something destructive. **Supervisor+** pauses the
  agent on a hard stop and shows a *Resume* banner in the panel.
- A small **narrator LLM** summarises decisions, file edits and reasoning in
  short human-voice lines. One checkbox picks the weakest model for the
  narrator and the strongest for the Supervisor from the CLI's own catalog.
- Keeps listening as the primary experience: hear what matters, read the trace
  when useful, and interrupt only when needed.
- A **browser popup** on localhost exposes Play/Pause/Mute/Speak/Interrupt
  buttons and settings.
- A **launcher** for ~25 coding CLIs (Claude Code, Codex, OpenClaw, OpenCode,
  Hermes, Droid, Pi, Cline, Copilot CLI, Oh My Pi, DeepSeek Harness, Qwen,
  Gemini, Aider, …) plus a plain proxied **Terminal** — one click each.
- **Push-to-talk** (default `Alt+Space`, *temporarily disabled*): your question
  goes to STT → then into the running agent as a native side-question, a queued
  next message, or to the clipboard (depending on CLI capability).
- **Pause the CLI** while you talk (`Alt+P` or auto-on-speak): the subprocess
  is suspended via `psutil`, no races with the agent.
- Works in English, Spanish, French, Ukrainian and Russian.
- Plug-in providers for TTS, STT and commentator LLM — run fully local or use
  cloud APIs.

## What Voice Copilot is not

- Not a prompt dictation app or voice keyboard
- Not a replacement for Claude Code, Codex, Gemini CLI, or other coding agents
- Not just text-to-speech for raw terminal logs
- Not direct verbatim reading of the model's hidden thinking or full answer
- Not another chat UI you need to stare at all day

## Install options

The Quickstart above uses the cloud-light default. To run models locally
instead, install the matching extra:

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

Codex works the same way: `voice-copilot run codex -p "explain what this repo does"`.

## Narrate _any_ CLI via proxy mode

The **Launch** tab does this for you. If you would rather wire it up by hand —
`voice-copilot run <target>` only knows `claude` and `codex`, so for everything
else (aider, opencode, Cline, GitHub Copilot CLI that hits OpenAI/Anthropic)
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
| DeepSeek   | `DEEPSEEK_BASE_URL`      | `api.deepseek.com`                         |
| Ollama     | `OLLAMA_BASE_URL`        | `127.0.0.1:11434` (local)                  |
| Gemini     | `GEMINI_BASE_URL`        | `generativelanguage.googleapis.com` (passthrough) |

OAuth-authenticated CLIs (Claude Code subscription, Codex login flow) work
out of the box — we only see the bearer token on the wire and forward it.
The OAuth browser round-trip happens on different domains that we don't
intercept.

## Hotkeys

| Action                       | Default combo     | Notes                                        |
| ---                          | ---               | ---                                          |
| Interrupt (pause & listen)   | `Alt+Shift+Space` | Suspends the CLI process.                    |
| Pause / resume toggle        | `Alt+P`           | Manual pause of the child CLI.               |
| Mute TTS                     | `Alt+M`           | Stops narration without affecting the agent. |
| Skip current narration       | `Alt+Shift+N`     | Drops the line being spoken, keeps the queue. |
| Push-to-talk                 | `Alt+Space`       | *Off in this build* — see voice input above. |

All of them are rebindable under **Settings → Hotkeys**.

## Providers

Every layer is pluggable. Defaults are cloud-light so `pipx install voice-copilot`
works out of the box.

|          | Default (light)       | Local (extra)              | Premium cloud        | Secret name              |
| ---      | ---                   | ---                        | ---                  | ---                      |
| **TTS**  | `edge-tts`            | `silero`, `piper`          | `elevenlabs`, `openai` | `ELEVENLABS_API_KEY`, `OPENAI_API_KEY` |
| **STT**  | `openai-whisper-api`  | `faster-whisper`           | `deepgram`           | `OPENAI_API_KEY`, `DEEPGRAM_API_KEY` |
| **LLM**  | `auto` (the launched CLI, no key); API: `anthropic` (Haiku) | `openai-compat` (Ollama)   | `openai`, `github-copilot` | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_COMPAT_API_KEY`, `GITHUB_COPILOT_TOKEN` |

Switch via the **Settings** page or by editing `~/.voice-copilot/config.yaml`.
Every backend, its install extra and default model: [docs/providers.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/providers.md).

## Configuration

- `~/.voice-copilot/config.yaml` — edited by hand or via the settings page.
- **Supervisor**: `commentator.supervisor.mode` = `off` / `watch` / `guard`,
  `commentator.supervisor.model`, `commentator.supervisor.every_n_tools`;
  `commentator.auto_tier_models: true` picks weakest/strongest models per
  CLI; `commentator.per_cli.<cli>.supervisor_mode` overrides one CLI. See
  [docs/supervisor.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/supervisor.md).
- Secrets live in the **OS keychain** (Credential Manager / Keychain / Secret
  Service) or in a `.env` next to where you run `voice-copilot` (copy
  [`.env.example`](https://github.com/conus-vision/voice-copilot/blob/main/.env.example)); shell exports take precedence.
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

See [docs/architecture.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/architecture.md).

## Roadmap

Voice Copilot is in its first alpha. The goal right now is to validate the core
idea with advanced users. Planned work:

- grow the Supervisor: richer checkpoints (diff-aware review, cost and time
  budgets), corrections injected straight into the agent, not only spoken
- improve narration quality, timing, and signal-to-noise ratio
- stabilize multi-session workflows and session switching
- expand structured integrations with more coding CLIs
- refine the companion interface together with CLI authors so it matches real
  integration needs
- improve advanced configuration, onboarding, and developer documentation
- explore richer host UIs such as VS Code while keeping the core lightweight

The core is open-source under MIT to maximize adoption, experimentation, and
community contributions. The CLI companion integration RFC lives in
[docs/cli-companion-interface.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/cli-companion-interface.md), with the
normative schema in
[docs/schemas/cli-companion-interface.schema.json](https://github.com/conus-vision/voice-copilot/blob/main/docs/schemas/cli-companion-interface.schema.json).

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

## Get involved

If Voice Copilot is useful to you, here's how to help it grow:

- ⭐ **Star this repo** — it's the clearest signal that the listening-first
  approach resonates, and it helps other engineers find the project.
- 🗣️ **Tell us how you use it** — open an issue or email
  [info@conus.vision](mailto:info@conus.vision). Real workflows shape the roadmap.
- 🔌 **Building a coding CLI?** Let's design the companion interface together
  (see the [integration RFC](https://github.com/conus-vision/voice-copilot/blob/main/docs/cli-companion-interface.md)).

Contact: [info@conus.vision](mailto:info@conus.vision) · [conus.vision](https://conus.vision)

## License

This repository is open-source under the MIT license.

That means individuals, teams, companies, and other open-source projects can
use, modify, fork, and redistribute the core with minimal friction.

See [LICENSE](https://github.com/conus-vision/voice-copilot/blob/main/LICENSE) and [LICENSING.md](https://github.com/conus-vision/voice-copilot/blob/main/LICENSING.md).
