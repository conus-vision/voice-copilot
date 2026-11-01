# Voice Copilot for Claude Code

A second model watches what Claude Code does: a cheap one narrates the work aloud, and a
stronger one checks it at a few moments in each task. If Claude says "fixed" while the
tests still fail, you hear about it. You can answer from the panel or by voice, and your
words reach Claude as your own message, mid-turn or while it's idle.

## What you need

This plugin only connects Claude Code to the Voice Copilot companion, a separate program
that does the narrating:

```sh
uv tool install voice-copilot    # or: pipx install voice-copilot
```

Without it the plugin prints one line at the start of a session and does nothing else.

To install the plugin from its own marketplace, type this at the prompt of a terminal session:

```
/plugin install voice-copilot --marketplace conus-vision/voice-copilot
```

## Commands

Type `vc` for the list. Each is a whole message, and never reaches the model: a message that
only mentions one, such as `vc off please`, goes to Claude as usual.

| Message | What it does |
|---|---|
| `vc off`, `vc on` | This session; add `all` for every session |
| `vc status` | How it stands, and where the panel is |
| `vc check` | Has the narrator and the supervisor look at the agent now, even if it has gone silent |
| `vc ask <question>` | Asks the second model, without bothering Claude |
| `vc log`, `vc cost` | What it said; what it has spent |
| `vc speed 1.5`, `vc volume 70`, `vc mute` | How it sounds |
| `vc model`, `vc review`, `vc detail`, `vc access` | Which models, and how much they do and read |
| `vc pause`, `vc go` | Stops Claude before its next action, and lets it go on |
| `vc settings` | Opens the settings window |

`/vc` does the same, and can act while a tool runs. You can also ask Claude to turn it on or off.
Under Supervisor+ Claude can't turn it off: only you can.

## What it runs and what it sends

The plugin is the readable TypeScript file `hooks/voice-copilot.ts`, which runs inside
Claude Code.

- **It sends** each session event to the companion at `127.0.0.1`: your prompts, tool calls
  with their input and output, and the path to the session's transcript. It sends nothing
  anywhere else.
- **It reads** `companion.json`, `switched-off` and `sessions/` from Voice Copilot's own folder, for the
  companion's port and access token and for the on/off state. It asks the companion for your
  messages every two seconds while a session is open, and before each tool call.
- **It runs** `voice-copilot-hook` when a session starts, which starts the companion if
  needed, and when a message starts with `vc`, to tell whether it is a command.
- **It can act on the agent:** it adds your messages to the conversation and starts a turn
  with them, refuses a tool call when a message was said during a pause, and holds the next
  tool call while the agent is paused.

The companion is where data leaves your machine, and only as you configure it. It sends
the events to the model that narrates: by default your own Claude Code login, or a local or
third-party model you pick. It sends the narration's text to the speech service you pick:
by default Microsoft Edge's online voices. Its source is in this
[repository](https://github.com/conus-vision/voice-copilot), along with the [details of the
plugin](https://github.com/conus-vision/voice-copilot/blob/main/docs/plugins.md).

Narration calls count toward your Claude Code usage, about 2.6k input tokens per call on
Haiku and 4.5k per review on Sonnet. `vc off` stops them.

## Requirements

Claude Code 2.1.314 or newer, and `voice-copilot` 0.2.0 or newer. The module API is early
access and may change, so a Claude Code update can stop the plugin until it is updated too.
If that happens, your session carries on as if it weren't installed.

## License

MIT.
