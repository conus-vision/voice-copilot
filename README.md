<p align="center">
  <img src="https://raw.githubusercontent.com/conus-vision/voice-copilot/main/public/logo.png" width="440" alt="Voice Copilot">
</p>

> **Listen to Control.** A second engineer watching your coding agent: it stops a dangerous
> command before it runs, asks the questions you didn't, and tells you how the work was really checked.

Coding agents now work for minutes at a time, and their mistakes are confident ones: a
force push, a migration edited after it already ran, a password stored as is, "all tests
pass" when none ran. Reading every line defeats the point of delegating; not reading them
means finding out afterwards.

Voice Copilot puts a second, stronger model beside the agent. **Supervisor+** keeps its
own record of the session, checks every command before it runs, works out what can go
wrong in this particular task, and closes the work with a review of what was done and how
it was checked. A cheap narrator tells you by voice what the agent is doing right now, so
you can look away and still step in while a mistake can be undone. It works with the
Claude Code or Codex you already log in to, with no extra keys.

<p align="center">
  <img src="https://raw.githubusercontent.com/conus-vision/voice-copilot/main/public/demo.gif" alt="A real session: Codex says its new tests passed, all three failed, and the Supervisor names the line that broke them" width="100%">
</p>

The loop above is a real session. Codex wrote that its new tests passed. All three
had failed, along with 83 others, and the Supervisor named the line that broke
them. The narrator and the Supervisor ran on local models there.

<p align="center">
  <a href="https://www.youtube.com/watch?v=7Ys5_RRrrLw">
    <img src="https://raw.githubusercontent.com/conus-vision/voice-copilot/main/public/poster.jpg" alt="Watch the Voice Copilot video: the agent said its new tests passed, all three had failed (3 min)" width="100%">
  </a>
</p>

<p align="center">
  <a href="https://www.youtube.com/watch?v=7Ys5_RRrrLw"><b>▶ Watch the 3-minute video</b></a>
  ·
  <a href="https://voice-copilot.conus.vision/">Website</a>
  ·
  <a href="#quickstart">Quickstart</a>
  ·
  <a href="https://github.com/conus-vision/voice-copilot/discussions">Discussions</a>
</p>

## Supervisor+: better work from the agent you already use

The agent writes the code; Supervisor+ makes sure it is the code you asked for. It never
writes code itself. Turn it on with `vc mode plus-narrator` in your CLI's chat (or
`vc mode plus` to keep it quiet until something matters).

- **Stops a dangerous command before it runs.** A delete outside the project, `git reset
  --hard`, a force push, `--no-verify`, `DROP TABLE`, a script piped from the internet, a
  write to `.env` or `~/.ssh`, a package publish: the command waits for **Run it** or
  **Refuse** in the panel, with an alarm. Or pause for a while, or only warn: `vc react`.
  The agent can't approve its own command.
- **Asks what you didn't.** The advisor keeps a short watch list for the task at hand: a
  sign-up form gets password hashing and duplicate emails, a schema change gets data
  loss, a one-line fix gets "why is it rewriting the module". When the agent gets near
  one, a note waits in the panel with an instruction you can send it (`vc send`).
- **Judges the work, not the agent's account of it.** A record kept by the code (which
  checks ran, how they ended, what changed after the last one that passed) is set
  against what the agent says. "Done, tests pass" with no test run after the last edit,
  a hard-coded special case, a swallowed error, edits outside the task: a warning, or a
  stop.
- **Closes the work with a spoken review:** what was done against what you asked, how it
  was checked, what is still open, and one next step.
- **Remembers the whole session at a ninth of the price.** Through the Claude CLI it keeps
  one running conversation and sends only what is new: about $0.0055 a review on Opus
  instead of $0.05.
- **A second opinion from another vendor, if you like:** Codex supervising Claude Code, or
  the other way round, each on its own subscription (`vc review cli codex`).

Measured, not assumed: on 32 checkpoints and 5 review sequences with a known right answer
the supervisor scores 43 of 44 on Opus, with no false alarms; the advisor 15–16 of 16 on
Opus and Sonnet; the command rules are a table of 117 commands, half of them everyday
forms that must not fire. How each part works, and what it can't see:
[docs/supervisor.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/supervisor.md).

## Why this exists

One prompt can now start minutes of autonomous work. The part you care about
(what the agent understood, what it changed, which risks it noticed) is buried in text
that scrolls faster than you can read. Reading all of it defeats the point of
delegating, and ignoring it means you learn about a bad turn after the damage is
done.

The agent itself is the worst judge of its own work: its context fills up, its earlier
assumptions go unquestioned, and it reports what it meant to do. A second model that
reads the session from outside, with its own record and none of that context, catches
what the first one talks itself past. Voice Copilot is that second model, and the voice
is how it reaches you without asking you to watch: it listens to the agent on your
behalf, says what matters, and lets you step in.

## How it works

Two models watch the agent's event stream, and they have opposite budgets.

The narrator is cheap: the weakest model your CLI offers, or Haiku 4.5 through
the API. Every few seconds it says what the agent is doing, in one or two
sentences. It summarizes what happened; it does not read the model's hidden
thinking aloud.

The Supervisor is the most capable model your CLI can run, and it only looks at
checkpoints: the end of a turn, a run of tool calls, a failure that repeats. It
gets the goal, its record of the session and what happened since its last look,
then answers `OK` or says what is wrong. Supervisor+ adds what runs between and
around those checkpoints: the check before each command, the advisor, the review at
the end and its own sounds.

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

## Quickstart (two commands)

> Goal: hear Voice Copilot narrate a real session. You need Python 3.11+ and a
> coding CLI you already log in to. With Claude Code or Codex there is no API key
> to find: the narrator and the Supervisor reuse that CLI's login by default.

**1. Install and connect:**

```bash
uv tool install voice-copilot       # or: pipx install voice-copilot
voice-copilot setup
```

*Expected:* it connects whichever of Claude Code, Codex, Pi, oh-my-pi and DeepSeek
Harness you have, asks to turn itself on, and plays a test line. The panel is at
`http://127.0.0.1:8765`. Click once in it if the browser blocks autoplay.

**2. Start your CLI the way you always do**, and give it a task.

*Expected:* it says `Voice Copilot is connected. For the list of commands, type vc`,
and within a few seconds you **hear** what the agent is doing. The panel's **Trace**
tab shows every event, line and verdict.

That's the loop: **start your CLI, listen, step in when it matters.**

**Any other CLI** goes through the local proxy, with the same commands: `vc gemini`,
`vc opencode`, `vc copilot`, `vc qwen`, `vc aider`. Or run `voice-copilot serve` and
press **Launch** next to a CLI on the panel's Launch tab, which needs no env vars.

> **Voice input is off in this build** while the push-to-talk → speech-to-text →
> agent path is reworked. Narration, the launcher and the trace work as usual.
> To turn voice input back on, set `voice_input: {enabled: true}` in the config
> file (`voice-copilot config` prints its path).

## Who it's for

Vibe coders build by feel, let the agent do the typing and rarely read the diff,
which is exactly when nobody asks whether the password is hashed or the migration
is safe. Supervisor+ asks for you: it works out what this task can get wrong, says
so when the agent gets near it, and hands you the instruction to send. The narrator
tells you in plain language what the agent decided and changed, so you keep track
without reading.

Professional engineers run long autonomous sessions with Claude Code or Codex
on real codebases, where the risk is a confident wrong turn buried in minutes
of output. Supervisor+ holds a destructive command until you answer, checks the
agent's "done" against what actually ran, and closes each turn with what is still
unchecked, so you can do something else and still interrupt early, instead of
reviewing a large bad diff afterwards. Listening also lets you follow more than one
session without watching every token.

Multitaskers and reviewers hand work off and need to know when to step in.
The narration runs in the background; when a line tells you something matters,
open the trace.

Authors of coding CLIs can have their tool expose a clean event stream for
companions like this one (see the integration RFC below).

> **Status: alpha, Supervisor+.** It is aimed at advanced users who are comfortable
> testing CLI workflows and sending feedback. Questions, ideas and sessions where it
> got something wrong are welcome in
> [Discussions](https://github.com/conus-vision/voice-copilot/discussions). Voice
> Copilot is made by Volodymyr Moskvin at [Conus Vision](https://conus.vision), and
> we welcome collaboration: [info@conus.vision](mailto:info@conus.vision).

## What it does

- Follows a coding CLI in real time: through the local proxy for the CLIs in
  the launcher, or through stream-JSON for Claude Code and Codex.
- Has a Supervisor, the strongest model your CLI can run, review the agent at
  checkpoints and warn you out loud when it drifts, goes in circles, touches
  files outside the task or calls unchecked work done. Supervisor+ also holds a
  dangerous command before it runs, raises the questions the task needs through
  its advisor, and closes the work with a spoken review (see above).
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
- Connects to Claude Code, Codex, Pi, oh-my-pi and DeepSeek Harness through
  plugins, so a CLI you start yourself is narrated with nothing rerouted.
  `voice-copilot setup` connects them. Nine more (OpenCode, Hermes Agent, Gemini
  CLI, Qwen Code, Copilot CLI, Grok Build, Droid, OpenHands, Kimi Code) report
  through their own hooks once `voice-copilot integrate` has set them up.
- Takes commands in your chat: type `vc` in any connected CLI for the list. Only
  a message that is exactly a command counts, so a sentence that mentions `vc`
  still goes to your agent.
- Lets you step in from the panel: on Claude Code your message reaches the agent
  as a real message in the middle of its turn. See
  [docs/plugins.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/plugins.md)
  for how it arrives on the other CLIs.
- Offers push-to-talk (`Alt+Space`, off in this build): your question goes
  through speech-to-text and is typed into the running agent, or queued for its
  next turn.
- Pauses the agent (`Alt+P`, or automatically while you speak) by suspending
  its process with `psutil`, so it cannot race ahead while you talk. Only the
  agent's own process is suspended; processes it started keep running.
- Narrates in English, Spanish, French, Ukrainian and Russian.
- Loads speech output, speech input and the narrator model as plug-ins. Each
  can run on your computer: the narrator on a local model (Ollama or another
  OpenAI-compatible server), speech output with Piper voices, speech input
  with faster-whisper.

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

# + local TTS (Piper voices through sherpa-onnx)
pipx install "voice-copilot[local-tts]"

# + local STT (faster-whisper)
pipx install "voice-copilot[local-stt]"

# + ElevenLabs TTS
pipx install "voice-copilot[elevenlabs]"

# every extra
pipx install "voice-copilot[all]"
```

With `local-tts`, pick `piper` as the voice in Settings. The extra adds about
45 MB and no PyTorch. The first line in each language downloads its voice once
(about 65 MB, from the sherpa-onnx releases on GitHub) into `voices/` next to
the config file; after that speech output needs no network. A short line takes
a fraction of a second on a laptop CPU. Silero and Deepgram are reserved names
without an engine yet, and selecting them says so.

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install voice-copilot
uvx voice-copilot run claude -p "refactor the auth module"
```

Codex works the same way: `voice-copilot run codex -p "explain what this repo does"`.

## Narrate _any_ CLI through the proxy

The **Launch** tab does this for you. To wire it up by hand
(`voice-copilot run <target>` only knows `claude` and `codex`), run the proxy as
a standalone service and point your CLI's base URL at it. That covers aider and
any other CLI that reads its endpoint from a base-URL variable:

```bash
voice-copilot proxy
# → prints ANTHROPIC_BASE_URL=http://127.0.0.1:8766/anthropic
#          OPENAI_BASE_URL   =http://127.0.0.1:8766/openai/v1
#          ...and OpenRouter / Groq / Mistral / Ollama / Gemini

# in another terminal:
ANTHROPIC_BASE_URL=http://127.0.0.1:8766/anthropic \
  aider --model anthropic/claude-sonnet-5
```

Not every CLI reads that variable. Crush reads `OPENAI_API_ENDPOINT`, Goose
needs `GOOSE_PROVIDER=openai`, Cline takes the URL through `cline auth`, and
Copilot CLI routes only bring-your-own-key models, through
`COPILOT_PROVIDER_BASE_URL`. Amp, Auggie and Droid's hosted models go to their
vendor whatever you set. The Launch tab says this for each CLI. Where a CLI has
hooks, `voice-copilot integrate <cli>` narrates it without the proxy.

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

## Use it as a plugin

Keep starting your CLI the way you always do, and let its plugin bring Voice Copilot along: the
first session starts it, and you can correct the agent mid-turn from the panel.

```sh
uv tool install voice-copilot
voice-copilot setup
```

`setup` connects Claude Code, Codex, Pi, oh-my-pi and DeepSeek Harness, whichever you have.
Any other CLI works through `vc <cli>`.

Then type `vc` in the chat for the list of commands: `vc off` for this session, `vc off all` for
every one, `vc check` to have it look at the agent right now, `vc ask` to ask the second model,
`vc speed`, `vc volume`, `vc model`, `vc review` and more. The model never sees those, and a
message that only mentions them goes to it as usual.

[docs/plugins.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/plugins.md)
covers all of them, how a plugin compares with `vc <cli>`, how your messages reach the agent and
how to add other CLIs.

Nine more CLIs report through their own hooks or plugin. Connect one and keep the panel running:

```bash
voice-copilot integrate gemini     # or opencode, hermes, qwen, copilot, grok, droid, openhands, kimi
voice-copilot serve
gemini                             # any terminal, as usual
```

The Plugins section of the panel's Settings tab has the same Install button per CLI. OpenCode,
Hermes Agent and Kimi Code were tested end to end; the others follow each CLI's hook reference.
Every step is in
[docs/integrations.md](https://github.com/conus-vision/voice-copilot/blob/main/docs/integrations.md).

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
| **TTS**  | `edge-tts`            | `piper`                    | `elevenlabs`, `openai` | `ELEVENLABS_API_KEY`, `OPENAI_API_KEY` |
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
refuses to save a provider it cannot start. Saved settings apply at once: the
narrator, the voice, speech input, hotkeys and the debounce. Only the panel's
host and port need a restart.

The way of working is two settings: `commentator.narrator` (on or off) and
`commentator.supervisor.mode` (`off`, `watch` for the Supervisor, `plus` for
Supervisor+; the old `guard` still reads as `plus`). `vc mode` sets both at once.
Supervisor+ reacts as `commentator.supervisor.react` says (`hold`, `pause` for
`pause_s` seconds, or `say`), and `commentator.supervisor.cli` runs it through
another CLI. `commentator.supervisor.model` and
`commentator.supervisor.every_n_tools` work as before.
`commentator.auto_tier_models: true` picks the weakest and strongest model per
CLI, and `commentator.per_cli.<cli>` overrides the mode, the model and the CLI
for one CLI. See
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
- run the hook integrations of Gemini CLI, Qwen Code, Copilot CLI and the
  others against each CLI, carry the `vc` words through them, and add Cline and Amp
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
