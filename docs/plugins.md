# Plugins

`vc claude` wraps the CLI and reads its traffic through a proxy. A plugin works
the other way round: you start the CLI as usual, in the terminal or in its IDE
extension, and it reports to Voice Copilot. Same narrator, same supervisor, same
panel.

```
 claude / codex / …                 voice-copilot companion
 ┌──────────────────┐  hook JSON   ┌───────────────────────────────┐
 │ plugin hooks     │ ───────────▶ │ HookAdapter → bus             │
 │ voice-copilot-   │              │   ├─ narrator (cheap model)   │
 │ hook <cli>       │ ◀─────────── │   ├─ supervisor (strong model)│
 └──────────────────┘  your words, │   └─ TTS → panel              │
                       or a hold   └───────────────────────────────┘
```

The first session starts the companion and opens the panel. It exits 15 minutes
after the last session ends.

## Install

```sh
uv tool install voice-copilot    # or: pipx install voice-copilot
voice-copilot setup
```

`setup` finds the CLIs on this machine and connects each one: Claude Code, Codex, Pi, oh-my-pi
and DeepSeek Harness. The plugins ship inside the package, so it needs no copy of this
repository. It asks before trusting Codex's hooks, which Codex otherwise wants done by hand
through `/hooks`, and before turning Voice Copilot on. Then it plays a test line.

Any other CLI, OpenCode included, works through the proxy with the same commands:
`vc opencode`.

To connect one by hand, from a checkout of this repository:

```sh
claude plugin marketplace add conus-vision/voice-copilot && claude plugin install voice-copilot@voice-copilot
codex plugin marketplace add conus-vision/voice-copilot && codex plugin add voice-copilot@voice-copilot
pi install ./plugins/pi
omp plugin install ./plugins/pi
dsh plugin --profile <name> add -w ./plugins/deepseek-harness    # for each profile you use
```

Plugins with hooks can't be listed in OpenAI's public directory, so a marketplace is the way
in for Codex.

Each new session says so when it connects: *Voice Copilot is connected. For the list of
commands, type vc.*

## Commands

Type these as the whole message in any CLI's chat. They never reach the model and cost no
tokens, with one exception on Codex below. The same words work in a terminal.

| Message | What it does |
|---|---|
| `vc` | Lists these |
| `vc off`, `vc on` | This session only, and it sticks until you change it |
| `vc off all`, `vc on all` | Every session, new ones included |
| `vc status` | How it stands, and where the panel is |
| `vc check` | Has the narrator and the supervisor look at the agent now |
| `vc ask <question>` | Asks the second model, without bothering the agent |
| `vc log` | The last things it said, as text |
| `vc cost` | How many calls it has made, and roughly how many tokens |
| `vc speed 1.5` | Speech speed, 0.5 to 3 |
| `vc volume 70` | Volume, 0 to 100 |
| `vc mute`, `vc unmute` | Voice off and on. The agent carries on |
| `vc pause`, `vc go` | Stops the agent before its next action, and lets it go on |
| `vc model`, `vc model 2` | Lists the narrator's models, and picks one |
| `vc model ollama/gemma4:12b` | Picks one by name, from any provider |
| `vc review off`, `watch` or `guard` | The supervisor. See [supervisor.md](supervisor.md) |
| `vc review model` | The same as `vc model`, for the supervisor |
| `vc detail low`, `normal` or `high` | How much the narrator says |
| `vc access events`, `edits` or `files` | What the models may read, below |
| `vc settings` | Opens the settings window |

Each also has a Russian name: `vc выкл все`, `vc скорость 1.5`, `vc модель`, `vc спроси ...`. Where
a word takes a value, on its own it shows the current one.

**A command, or something you said about one?** A message is a command only if all of it fits,
on one line. `vc off` is a command. `vc off please`, `vc status of this repo`, `what does vc off do?`
and a paste that starts with `vc off` go to the agent untouched. To say those exact words to the
agent, add a word.

**Why not a slash command.** Codex rejects an unknown one before any hook sees it, so `vc` is
what works everywhere. Claude Code, Pi, oh-my-pi and DeepSeek Harness also take `/vc`, with its
place in their menu. Claude Code and Pi offer a picker when a command has choices.

**Keys.** `vc model` with a model that needs an API key opens the settings window, and takes
the key there. A key typed in the chat would stay in the transcript and in the CLI's history.

**The agent stuck?** Nothing else in Voice Copilot moves until an event arrives, so an agent
stuck inside a tool gets no line and no review. `vc check` gives up on any model call in flight,
has the narrator say what the agent is waiting on and for how long, and runs a review. A warning
you already heard is said again, since you asked. It's also the **?** button in the panel and
`Alt+Shift+C`.

**While a tool runs.** Whether a message can act at once depends on the CLI:

| | Acts during a tool | |
|---|---|---|
| Claude Code, terminal | Yes | |
| Claude Code, VS Code panel | `/vc` yes, `vc` no | `vc` waits for the tool |
| Pi, oh-my-pi | Yes | |
| Codex, DeepSeek Harness | No | They hold what you type until the tool finishes |

So for a stuck tool on those, use the panel, `Alt+Shift+C` or a terminal. The same goes for
`vc go`: if the prompt can't get through, `Alt+P` and the panel's button always can.

**Mid-turn on Codex.** Blocking a prompt typed during a turn would end that turn there. So any
of these typed mid-turn does go to the model, with a note telling it to ignore them and carry
on. That costs one model step, and the agent then finishes its work.

**Asking the agent.** "Turn Voice Copilot off" works too, through a skill in the plugin, and
means its own session. It costs a model call, and the CLI shows you that the agent did it. Under
Supervisor+ the agent is refused, since the point is to stop that agent: only you can turn it off.

These words aren't a security boundary. An agent with a shell can run the same commands, or edit
the switch file, if its sandbox lets it.

**Codex's own switch.** `/plugins`, then Space on Voice Copilot, also stops the hooks at once, even
mid-turn. That edits your Codex config rather than Voice Copilot's state.

### What the models may read

| `vc access` | The narrator | The supervisor and `vc ask` |
|---|---|---|
| `events` | Tools, commands, their output | The same |
| `edits` (default) | The same | Also the last six edits, up to 1,200 characters each |
| `files` | The same | Also the last three files the agent changed, up to 6,000 characters each |

`files` reads only inside the session's folder, and skips what looks like a secret, such as `.env`,
`*.key` or `credentials.json`. Whatever is read goes to the model doing the reading, so on a cloud
model that means your code. `vc access files` says which model it is.

### When nobody is listening

If the panel is closed, a supervisor's warning goes to the CLI's chat instead, so it isn't lost.
Claude Code, Pi and oh-my-pi show it at once. Codex shows it with its next hook, which after the
last turn means your next prompt. Through the proxy there is no way to show it between turns.

### Through the proxy

A CLI launched with `vc <cli>` takes the same words. The proxy answers in the model's place: nothing
goes upstream and no token is spent, and the CLI shows the reply like any other. It stays in that
CLI's history as a short exchange. `vc off` stops the narration, and the CLI carries on through
the proxy. `vc pause` freezes the process, so only `Alt+P` or the panel can resume it. A full-screen
CLI may wipe the line saying it's connected: the panel says so too.

## What you get

| | Plugin | `vc <cli>` (proxy) |
|---|---|---|
| Works with a CLI you already started | Yes | No |
| Works in the IDE extension | Claude Code, Codex | No |
| Tools, files, the agent's text between steps | Yes | Yes |
| Reasoning | Only if the CLI records it (see below) | What the API stream carries |
| Corrections | Mid-turn: as your own message where the CLI allows it, else relayed as hook text | Typed into the terminal as your own words, so at the next turn |
| `vc` commands | Yes | Yes |
| Pause the agent | At its next tool call | Freezes the process |

A CLI you launch through `vc` is narrated through the proxy only: hooks seen
inside it stay silent, and so do the narrator's own `claude -p` calls.

**Reasoning.** Transcripts hold it only when the CLI is told to keep it. By default
Claude Code stores a signature with no text, and Codex stores encrypted content
with an empty summary. Set `showThinkingSummaries` in Claude Code, or
`model_reasoning_summary` in Codex, to give the narrator more to work with.

## How your message reaches the agent

A plugin that runs inside the CLI says your words as a user message:

| CLI | While it works | While it's idle |
|---|---|---|
| Claude Code | A user-role row the model reads at its next step | A turn of its own, shown as *Prompt from the voice-copilot plugin* |
| Pi | A steer | A new prompt |
| oh-my-pi | An aside, which leaves the running tools alone | A new prompt |
| DeepSeek Harness | `agent.steer()` | `agent.followup()` |
| OpenCode | `client.session.promptAsync`, read at its next step | The same call, which starts a turn |

The plugin asks the companion for your messages before each tool call and every second or
two. Claude Code's IDE extension always keeps a debug log, so there each request adds two lines
to it. Measured on Claude Code
2.1.314, a message said at the first of three file reads:

| Said mid-turn | Haiku | Sonnet |
|---|---|---|
| "Skip the README, I changed my mind" | 2 of 2 | 2 of 2 |
| "Also end your final answer with the exact word BANANA" | 2 of 2 | 2 of 2 |

As hook text, that second one was declined (below). The same two messages on a local
`qwen3.5:9b`, where a step can take under a second:

| Said mid-turn | Pi | oh-my-pi | DeepSeek Harness | OpenCode |
|---|---|---|---|---|
| "Skip the README" | 3 of 3 | 3 of 3 | 2 of 3 | 1 of 3 |
| "End with the word BANANA" | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |

When the README was read, the model had usually asked for all three files at once, before
the message arrived. Across 15 OpenCode runs, including earlier builds of the plugin, it was
read in all 4 runs with parallel calls and in none of the 11 sequential ones. These are small
samples.

Nothing can enter a model's response while it is being generated, and no message stops
calls already running beside the one it reached.

During a pause your message stays with the companion, and the held tool call is refused with
it as the reason: that call was about to run, and you may be objecting to it.

The module is an early-access Claude Code API. Should it stop working, the plugin stops with
it and your session carries on without Voice Copilot.

**Codex and other hook-only CLIs.** Here your message waits for the next hook:

| Hook | What the agent gets | Timing |
|---|---|---|
| `PreToolUse` | The tool call is refused, with your message as the reason | Before its next action |
| `Stop` | The turn continues with your message as its prompt | When it was about to finish |
| `UserPromptSubmit` | Context beside the prompt you typed | The session was idle, and the CLI can't be woken |

Codex can also be woken: a message said while its interactive session is idle goes
through `codex queue` and starts a new turn as your own words, about 3 s later. That
works in the terminal, not in `codex exec`, which has ended by then anyway.

The panel's Trace shows when a message went out.

**Why a refused tool call.** Hooks can also add context after a tool result, but
models often take that for an injection from the tool's output. Measured on Claude
Code 2.1.314, with the correction "Skip the README, I changed my mind", and counting
the runs where the README then went unread:

| Route | Haiku | Sonnet |
|---|---|---|
| Context after a tool result | 1 of 4 | 2 of 3 |
| Refused tool call | 4 of 4 | 3 of 3 |

Ending the turn is more robust still: through `Stop`, even an instruction unrelated to
the task ("end your answer with the word BANANA") was followed in 7 of 7 runs.

**A message can still be declined.** That same unrelated instruction was refused when
it came as a refused tool call (one run, Haiku). The model can't tell a hook's text
from a file that says the same words, so it weighs mid-turn text against its task.
That is the safeguard working, so Voice Copilot doesn't try to talk its way past it.
Corrections about the task are what this channel is for.

**On Codex** the same routes were measured on 0.155.1:

| Route | `gpt-6-luna` | `qwen3.5:9b` (Ollama) |
|---|---|---|
| Refused tool call: "Skip the README" | 4 of 4 | 2 of 3 |
| `Stop`: "end your answer with BANANA" | 3 of 3 | 3 of 3 |
| Next prompt: "Answer in French" | 3 of 3 | 0 of 3 |

The one refusal that failed on `qwen3.5:9b` wasn't declined: the model had started
three commands at once, the refusal landed on the second, and the third was already
reading the README. A refusal stops one call, not the ones running beside it.
`Stop` reaches Codex as a user message and the next prompt as a developer one, which
that small model didn't follow.

These are small samples.

## Pausing

Supervisor+, the pause hotkey and *Hold while narrating* all use the same hold: the
next `PreToolUse` hook doesn't answer until you resume. The agent stops before its
next action, though text it is already generating finishes. A CLI kills a hook after
600 s, so the hold lets go at 540 s and says so in the panel. The next tool call is
then held again.

## Other CLIs

Hook vocabularies have converged on Claude Code's, so a new CLI is usually a manifest
plus `voice-copilot-hook <name>`. A CLI with plugins of its own can do better: post those same
events to `/api/hooks/<name>`, and read your messages from `/api/hooks/<name>/mail`.
`plugins/pi/voice-copilot.ts` is the shortest example. The name selects the narration
profile when Commentator is set to reuse the CLI; a CLI without a profile needs a provider
picked in the Commentator tab. Pi and oh-my-pi have one, which uses your default model with
no tools, no extensions and no session saved.

**DeepSeek Harness** (tested on 0.2.1-rc.5, a developer preview that warns of breaking
changes). The plugin reports tools, text and thinking, and can hold or refuse a tool call.
Answers to `vc` words go to stderr, and to the transcript for `/vc`. dsh has no narration profile, because each
`dsh --profile headless` run saves a session, so pick a provider in the Commentator tab.

**OpenCode** (tested on 1.19.3). Use `vc opencode`: through the proxy the commands are answered
cleanly. There is also `plugins/opencode/voice-copilot.ts`, which `setup` doesn't install: copy it
to `~/.config/opencode/plugins/`. It can deliver your messages as real ones and hold a tool call,
but a plugin can stop a prompt only by throwing, so every `vc` word shows as a failed prompt
before the answer appears, as a toast or on stderr under `opencode run`.

**oh-my-pi** (tested on 18.15.0, which needs Bun 1.4.4). It runs Pi's plugin, the same file,
which allows for what the fork changed. `vc` words work in its terminal and RPC modes but not
under `omp -p`, where the fork doesn't raise `input`. It narrates with your default model and
no tools.

**A fork** isn't worth it for any of these: a plugin already gets the events, the gate and
real user messages, and a fork has to follow projects that change daily.

## Limits

- On Codex, a mid-turn message is relayed through a hook, not typed by you, so it
  carries less weight. See above.
- A long-running tool delays a message until it finishes.
- Messages aren't handed to sub-agents; they wait for the main agent.
- Codex's hooks don't say how a command ended, and a command that runs past about 10 s
  gets no `PostToolUse` at all. Both come from its transcript on the next hook, so a failure
  reaches the narrator a step late.
- Codex's VS Code panel, in code mode, writes neither to its transcript: a failed command
  there looks like an ok one with no output.
- Transcript formats are private to each CLI. A format change costs the narration its
  detail between tools, and hooks keep working.
- The panel is still where audio plays, so the first narration may need a click in the
  tab if the browser blocks autoplay.
