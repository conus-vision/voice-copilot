<p align="center">
  <img src="https://raw.githubusercontent.com/conus-vision/voice-copilot/main/public/logo.png" width="440" alt="Voice Copilot">
</p>

> **Listen to Control.** Hear what your coding agent is doing, and get warned when it goes off track.

Coding agents now work for minutes at a time: they read files, edit code and
call tools. You can watch the terminal the whole time, or you can look away and
find out about a wrong turn after it happened. Voice Copilot lets you look away
and still hear about it in time. A cheap model tells you, in a sentence or two,
what the agent is doing right now. A stronger model, the Supervisor, reviews the
work at checkpoints, speaks up when the agent drifts, and in Supervisor+ mode
pauses it until you decide what happens next.

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

One prompt can now start minutes of autonomous work. The part you care about
(what the agent understood, what it changed, which risks it noticed) is buried in text
that scrolls faster than you can read. Reading all of it defeats the point of
delegating, and ignoring it means you learn about a bad turn after the damage is
done.

Plain text-to-speech reads logs word for word, which is noise. Voice-coding
tools help you dictate prompts to the agent but tell you nothing about what it
does next. Voice Copilot works in the other direction: it listens to the agent
on your behalf, turns the stream into short spoken updates you can follow with
your eyes off the screen, and gives you a way to interrupt when you want to
steer.

## How it works

Two models watch the agent's event stream, and they have opposite budgets.

The narrator is cheap: the weakest model your CLI offers, or Haiku 4.5 through
the API. Every few seconds it says what the agent is doing, in one or two
sentences. It summarizes what happened; it does not read the model's hidden
thinking aloud.

The Supervisor is the most capable model your CLI can run, and it only looks at
checkpoints: the end of a turn, a run of tool calls, a failure that repeats. It
gets the goal, a running summary and the recent transcript, then answers `OK`
or says what is wrong. In Supervisor+ mode it also pauses the agent and waits
for you.

```
 coding agent  ──►  event stream  ──►  narrator (cheap)     ──►  spoken update        ──►  you
 (Claude/Codex)     (json / proxy)     supervisor (strong)  ──►  warning · agent paused     (listen · resume · interrupt)
```

With Claude Code, Codex, OpenCode, Gemini CLI and Copilot CLI, both models run
through the CLI you launched, with its login and its models, so you need no
extra API key. Other CLIs need an API provider, which you choose in the
Commentator tab.
Oversight costs a small fraction of one agent turn, and you hear about a bad
turn while it can still be stopped. The details are in
[docs/supervisor.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/supervisor.md).

<a id="quickstart"></a>

## Quickstart (about 3 minutes to the first narration)

> Goal: hear Voice Copilot narrate a real Claude Code session. You need Python
> 3.11+ and a coding CLI you already sign in to (Claude Code, Codex, OpenCode,
> Gemini CLI or Copilot CLI). You don't need an extra API key, because the
> narrator and the Supervisor reuse that CLI.

**1. Install.** The default install uses cloud speech services and downloads no
local models:

```bash
pipx install voice-copilot          # or: uv tool install voice-copilot
```

**2. Open the panel.** This starts the local server and opens the panel in your
browser:

```bash
voice-copilot serve
```

You should see `http://127.0.0.1:8765` open on the **Launch** tab, with every
coding CLI found on your machine in the list.

**3. Launch an agent from the panel.** Choose the working folder, then press
**Launch** next to Claude Code (or Codex, OpenCode, Droid, Cline, Copilot CLI
and others). The CLI opens in a new terminal that already goes through the
local proxy, so there are no environment variables to copy. From your own shell,
`vc claude` does the same thing in the current folder.

The panel then shows a live trace, and within a few seconds you hear a short
spoken summary of what the agent is doing. If the browser blocks autoplay, click
once anywhere in the panel.

That is the whole loop: launch, listen, and open the trace when you want the
details.

> **Voice input is off in this build** while the push-to-talk → speech-to-text →
> agent path is reworked. Narration, the launcher and the trace work as usual.
> To turn voice input back on, set `voice_input: {enabled: true}` in the config
> file (`voice-copilot config` prints its path).

## Who it's for

Vibe coders build by feel and let the agent do most of the typing, and a fast
wall of diffs and tool calls breaks the flow. Voice Copilot tells you in plain
language what the agent decided and changed, so you keep track of where it is
going and can step in the moment it drifts.

Professional engineers run long autonomous sessions with Claude Code or Codex
on real codebases, where the risk is a confident wrong turn buried in minutes
of output. Voice Copilot tells you the root cause, the risk and the next step as
the work happens, so you can do something else and still interrupt early,
instead of reviewing a large bad diff afterwards. Listening also lets you follow
more than one session without watching every token.

Multitaskers and reviewers hand work off and need to know when to step in.
The narration runs in the background; when a line tells you something matters,
open the trace.

Authors of coding CLIs can have their tool expose a clean event stream for
companions like this one (see the integration RFC below).

> **Status: 0.1.0 alpha, the Supervisor release.** It is aimed at advanced users
> who are comfortable testing CLI workflows and sending feedback. Voice Copilot
> is made by Volodymyr Moskvin at [Conus Vision](https://conus.vision), and we
> welcome collaboration: [info@conus.vision](mailto:info@conus.vision).

## What it does

- Follows a coding CLI in real time: through the local proxy for the CLIs in
  the launcher, or through stream-JSON for Claude Code and Codex.
- Has a Supervisor, the strongest model your CLI can run, review the agent at
  checkpoints and warn you out loud when it drifts, goes in circles, touches
  files outside the task or runs something destructive. Supervisor+ also pauses
  the agent on a hard stop and shows a Resume banner in the panel.
- Uses a small narrator model to sum up decisions, file edits and reasoning in
  short spoken lines. One setting picks the CLI's weakest model for the
  narrator and its strongest for the Supervisor, from the CLI's own catalog.
- Treats listening as the main experience: you hear what matters, read the
  trace when it helps, and interrupt only when you need to.
- Runs a local panel in the browser with play, pause, mute, skip and interrupt
  buttons, the live trace and all settings.
- Launches about 25 coding CLIs with one click each (Claude Code, Codex,
  OpenClaw, OpenCode, Hermes, Droid, Pi, Cline, Copilot CLI, Oh My Pi, DeepSeek
  Harness, Qwen, Gemini, Aider and more), plus a plain Terminal with every proxy
  route set. A CLI is narrated when its model traffic goes through the proxy; a
  CLI that only talks to its vendor's own service still launches, but gives the
  narrator nothing to work with.
- Plugs into the plugin and hook systems of twelve CLIs (Claude Code, Pi,
  OpenCode, Hermes Agent, Codex, Gemini CLI, Qwen Code, Copilot CLI, Grok
  Build, Droid, OpenHands, Kimi), so they are narrated without the proxy and
  answer to pause, stop and voice messages. `voice-copilot integrate` sets one
  up.
- Offers push-to-talk (`Alt+Space`, off in this build): your question goes
  through speech-to-text and is typed into the running agent, or queued for its
  next turn.
- Pauses the agent (`Alt+P`, or automatically while you speak) by suspending
  its process with `psutil`, so it cannot race ahead while you talk. Only the
  agent's own process is suspended; processes it started keep running.
- Narrates in English, Spanish, French, Ukrainian and Russian.
- Loads speech output, speech input and the narrator model as plug-ins. The
  narrator can run on a local model (Ollama or another OpenAI-compatible
  server) and speech input can run locally with faster-whisper; speech output
  currently needs a cloud service.

## What Voice Copilot is not

It is not a dictation app or a voice keyboard, and it does not replace Claude
Code, Codex, Gemini CLI or any other coding agent. It does not read raw terminal
logs, the model's hidden thinking or its full answers aloud. It is also not one
more chat window that you have to watch all day.

## Install options

The Quickstart uses the light default. Extras add optional backends:

```bash
# light default: cloud TTS and STT
pipx install voice-copilot

# + local STT (faster-whisper)
pipx install "voice-copilot[local-stt]"

# + ElevenLabs TTS
pipx install "voice-copilot[elevenlabs]"

# every extra, including local-tts (see the note below)
pipx install "voice-copilot[all]"
```

The `local-tts` extra installs PyTorch for the Silero and Piper voices, but
neither engine is implemented yet, and neither is Deepgram speech input.
Selecting one of them reports that it is not available. For now, speech output
comes from edge-tts (free), OpenAI or ElevenLabs.

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install voice-copilot
uvx voice-copilot run claude -p "refactor the auth module"
```

Codex works the same way: `voice-copilot run codex -p "explain what this repo does"`.

## Narrate a CLI through its own plugin

Most coding CLIs can load a plugin or run hooks, and Voice Copilot ships one
for twelve of them. The CLI then reports from the inside: your prompt, the
answer, every tool call with its result, and the moment it waits for your
permission. Nothing is rerouted, so the CLI keeps its own login and endpoint.
The plugin also takes the controls back to the CLI: pause holds the agent at
its next tool call, a Supervisor STOP refuses its tool calls, and a voice
message reaches the model while it works.

For one session there is nothing to set up: `vc claude`, `vc pi` and the
Launch tab load the plugin themselves. To narrate a CLI you start on your own,
connect it once and keep the panel running:

```bash
voice-copilot integrate claude     # or pi, opencode, hermes, codex, gemini, qwen, ...
voice-copilot serve
claude                             # any terminal, as usual
```

The panel's Plugins tab has the same Install button per CLI and shows which
ones are connected right now. Claude Code, Pi, OpenCode and Hermes Agent were
tested end to end; the hook setups for Codex, Gemini CLI, Qwen Code, Copilot
CLI, Grok Build, Droid, OpenHands and Kimi follow each CLI's hook reference.
Every step, per CLI, is in
[docs/integrations.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/integrations.md).

## Narrate _any_ CLI through the proxy

The **Launch** tab does this for you. To wire it up by hand
(`voice-copilot run <target>` only knows `claude` and `codex`), run the proxy as
a standalone service and point your CLI's base URL at it. That covers aider,
opencode, Cline, GitHub Copilot CLI when it calls OpenAI or Anthropic, and any
other CLI that reads its endpoint from a base-URL variable:

```bash
voice-copilot proxy
# → prints ANTHROPIC_BASE_URL=http://127.0.0.1:8766/anthropic
#          OPENAI_BASE_URL   =http://127.0.0.1:8766/openai/v1
#          ...and OpenRouter / Groq / Mistral / Ollama / Gemini

# in another terminal:
ANTHROPIC_BASE_URL=http://127.0.0.1:8766/anthropic \
  aider --model anthropic/claude-sonnet-5
```

The panel lists each client that connects. Claude Code, Codex, OpenCode, Crush
and Cline send a session id with every request, so two terminals running the
same CLI show up as two entries; other clients are told apart by User-Agent
and API key. Pick one in the header dropdown to narrate it; the others keep
running silently.

Supported upstream providers:

| Provider   | Env var                  | Upstream                                   |
| ---        | ---                      | ---                                        |
| Anthropic  | `ANTHROPIC_BASE_URL`     | `api.anthropic.com`                        |
| OpenAI     | `OPENAI_BASE_URL`        | `api.openai.com`                           |
| ChatGPT plan (Codex) | `openai_base_url` config value (`-c openai_base_url=…`) | `chatgpt.com/backend-api/codex` |
| OpenRouter | `OPENROUTER_BASE_URL`    | `openrouter.ai/api`                        |
| Groq       | `GROQ_BASE_URL`          | `api.groq.com/openai`                      |
| Mistral    | `MISTRAL_BASE_URL`       | `api.mistral.ai`                           |
| DeepSeek   | `DEEPSEEK_BASE_URL`      | `api.deepseek.com`                         |
| DeepSeek, Anthropic format (DeepSeek Harness) | `DEEPSEEK_ANTHROPIC_BASE_URL` (DeepSeek Harness reads it as `DEEPSEEK_BASE_URL`) | `api.deepseek.com/anthropic` |
| Ollama     | `OLLAMA_BASE_URL`        | `127.0.0.1:11434` (local)                  |
| Gemini     | `GEMINI_BASE_URL` (Gemini CLI reads `GOOGLE_GEMINI_BASE_URL`) | `generativelanguage.googleapis.com` (passed through, not narrated yet) |
| OpenCode Zen | `OPENCODE_CONFIG_CONTENT` (the launcher writes it) | `opencode.ai/zen/v1`             |

CLIs that sign in with OAuth (a Claude Code subscription, the Codex login flow)
work as they are: the proxy only sees the bearer token on the wire and forwards
it. The OAuth round trip in the browser happens on other domains, which the
proxy does not touch.

If you already point a CLI at another endpoint (an Anthropic-compatible vendor
or a company gateway), `vc` keeps it: the proxy forwards to your endpoint
instead of the default upstream above, and the panel says so.

## Hotkeys

| Action                       | Default combo     | Notes                                        |
| ---                          | ---               | ---                                          |
| Interrupt (pause & listen)   | `Alt+Shift+Space` | Suspends the CLI process and stops the line being read. |
| Pause / resume toggle        | `Alt+P`           | Manual pause of the child CLI.               |
| Mute TTS                     | `Alt+M`           | Stops narration without affecting the agent. |
| Skip current narration       | `Alt+Shift+N`     | Drops the line being spoken, keeps the queue. |
| Push-to-talk                 | `Alt+Space`       | Off in this build (see the voice input note above). |

You can rebind all of them under **Settings → Hotkeys**.

## Providers

Every layer is pluggable. The defaults use cloud services, so
`pipx install voice-copilot` works without extra setup.

|          | Default (light)       | Local (extra)              | Premium cloud        | Secret name              |
| ---      | ---                   | ---                        | ---                  | ---                      |
| **TTS**  | `edge-tts`            | `silero`, `piper` (not implemented yet) | `elevenlabs`, `openai` | `ELEVENLABS_API_KEY`, `OPENAI_API_KEY` |
| **STT**  | `openai-whisper-api`  | `faster-whisper`           | `deepgram` (not implemented yet) | `OPENAI_API_KEY`, `DEEPGRAM_API_KEY` |
| **LLM**  | `auto` (the launched CLI, no key); API: `anthropic` (Haiku) | `openai-compat` (Ollama)   | `openai`, `github-copilot` | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_COMPAT_API_KEY`, `GITHUB_COPILOT_TOKEN` |

Switch providers in the **Settings** tab or in the config file. Every backend,
its install extra and its default model are listed in
[docs/providers.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/providers.md).

## Configuration

The config file lives in your platform's config folder:
`~/.config/voice-copilot/config.yaml` on Linux,
`~/Library/Application Support/voice-copilot/config.yaml` on macOS and
`%LOCALAPPDATA%\voice-copilot\config.yaml` on Windows. `voice-copilot config`
prints the exact path, and the launcher profiles sit next to it in
`proxy-cli.yaml`. You can edit both by hand or in the Settings tab, which
refuses to save a provider it cannot start.

The Supervisor is configured with `commentator.supervisor.mode` (`off`, `watch`
or `guard`), `commentator.supervisor.model` and
`commentator.supervisor.every_n_tools`. `commentator.auto_tier_models: true`
picks the weakest and strongest model per CLI, and
`commentator.per_cli.<cli>.supervisor_mode` overrides the mode for one CLI. See
[docs/supervisor.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/supervisor.md).

Secrets live in the OS keychain (Credential Manager, Keychain or Secret
Service) or in a `.env` file next to where you run `voice-copilot` (copy
[`.env.example`](https://github.com/conus-vision/voice-copilot/blob/main/.env.example)).
Shell exports take precedence. Values from `.env` are only for Voice Copilot:
the agent it launches does not see them.

There are no fallbacks between providers. If the configured one fails, the
error shows up in the panel and narration stops, so a broken setup is never
silent.

The panel and the proxy listen on `127.0.0.1`. To open the panel from a phone
or another computer, start it with `VOICE_COPILOT_HOST=0.0.0.0 voice-copilot serve`.
It prints a link with an access token. Another device needs that link once;
its browser then keeps the token in a cookie. Your own computer needs no token.
On a machine you share with other people, set `VOICE_COPILOT_TOKEN` to a
secret of your choice: every request then has to carry it, the panel opens
with it, and the hooks and plugins send it from the same variable.

## Interception strategies

1. Stream-JSON mode of the target CLI, used by `voice-copilot run claude` and
   `run codex`.
2. An HTTP reverse proxy (`voice-copilot proxy`, the launcher, `vc`, or
   `run … --proxy`). The CLI's base URL points at a local port, so the proxy sees
   the raw model stream, including `thinking` blocks that the terminal UI may
   hide. It works with any CLI that reads its endpoint from a base-URL variable
   or setting. The CLI talks plain HTTP to localhost and the proxy talks HTTPS
   upstream, so there are no CA certificates to install and no TLS
   interception. When `--proxy` runs next to a stream-JSON adapter, the adapter
   drops its own copies of the model events and the proxy is the single
   source.
3. The `vc` terminal wrapper runs any program in a real terminal and narrates
   it through the proxy when the CLI is in the catalog. An unknown CLI runs
   without narration until you add a proxy profile for it.
4. The CLI's own plugin or hooks report to the panel at
   `/api/companion/v1` (see
   [docs/integrations.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/integrations.md)).
   This needs no proxy, sees permission prompts and tool results, and is the
   only strategy that can hold or stop a CLI running in a terminal Voice
   Copilot did not start.

See [docs/architecture.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/architecture.md).

## Roadmap

Voice Copilot is in its first alpha, and the goal right now is to test the core
idea with advanced users. Planned work:

- grow the Supervisor: richer checkpoints (diff-aware review, cost and time
  budgets) and corrections delivered to the agent as well as spoken
- improve the quality and timing of narration and cut its noise
- make multi-session workflows and session switching stable
- run the hook integrations of Codex, Gemini CLI, Qwen Code, Copilot CLI and
  the others against each CLI, and add Oh My Pi, Cline and Amp
- refine the companion interface with CLI authors so it fits real integrations
- improve advanced configuration, onboarding and developer documentation
- explore richer host UIs such as VS Code while keeping the core small

The core is MIT-licensed so that adopting it and contributing to it stay easy.
The CLI companion integration RFC is in
[docs/cli-companion-interface.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/cli-companion-interface.md), and its
normative schema is in
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

- No voice output: most browsers allow autoplay only after one click, so click
  anywhere in the panel. If that doesn't help, open DevTools in the panel and
  check that `audio_header` frames and audio bytes arrive over the WebSocket.
- The narrator stays silent: check `commentator.min_importance` in Settings and
  set it to `low` to hear everything while you debug. When several `vc`
  sessions run at once, only one of them narrates: on Windows the one whose
  terminal or panel has focus, on macOS and Linux the one whose panel had focus
  last (see `focus.narrate_only_when_focused`).
- The panel answers `403 unexpected Host header`: it only accepts `localhost`,
  IP addresses and the host it was started with, which keeps other web pages
  out. Open it as `http://127.0.0.1:<port>`.
- The panel says it needs its access link (HTTP 401): it was opened from
  another device without the token. Open the link `voice-copilot serve`
  printed at start, the one that ends in `?token=`. The token is kept in
  `panel-token` next to the config file.
- Microphone denied: browsers only allow the mic on a trusted origin such as
  `http://127.0.0.1:<port>`. Don't serve the panel from a LAN IP without HTTPS.
- `keyring` finds no backend on headless Linux: install `keyrings.alt` into
  the same environment (`pipx inject voice-copilot keyrings.alt`), or put the
  keys in environment variables.

## Get involved

- Star the repo. It is the clearest sign that listening to an agent is useful
  to people, and it helps other engineers find the project.
- Tell us how you use it: open an issue or write to
  [info@conus.vision](mailto:info@conus.vision). Real workflows shape the roadmap.
- If you build a coding CLI, let's design the companion interface together (see
  the [integration RFC](https://github.com/conus-vision/voice-copilot/blob/main/docs/cli-companion-interface.md)).

Contact: [info@conus.vision](mailto:info@conus.vision) · [conus.vision](https://conus.vision)

## License

The code is released under the MIT license, so individuals, teams, companies
and other open-source projects can use, modify, fork and redistribute it with
little friction.

See [LICENSE](https://github.com/conus-vision/voice-copilot/blob/main/LICENSE) and [LICENSING.md](https://github.com/conus-vision/voice-copilot/blob/main/LICENSING.md).
