# Plugins and hooks: narrate a CLI from the inside

Most coding CLIs can load a plugin or run hooks. Voice Copilot ships one for
each CLI that allows it, and the CLI then reports what it does from the inside:
your prompt, the answer as it streams, every tool call with its result, and the
moment it waits for your permission. Nothing is rerouted through the proxy, so
the CLI keeps its own login, endpoint and features.

The plugin also carries the controls back. Pause holds the agent at its next
tool call, a Supervisor STOP refuses its tool calls until you take over, and a
message you say by voice reaches the model while it works.

## Quick start

For one session there is nothing to install. `vc claude`, `vc pi` and the
Launch tab in the panel load the plugin for that session.

For every session, connect the CLI once and keep the panel running:

```bash
voice-copilot integrate claude      # or pi, opencode, hermes, codex, gemini, ...
voice-copilot serve                 # the panel on http://127.0.0.1:8765
claude                              # use the CLI as usual, in any terminal
```

The panel's Plugins tab has the same Install button for each CLI, and shows
"connected now" after the first prompt. `voice-copilot integrate` with no
argument lists every integration and whether it is installed.

## What each CLI gives

| CLI | How it connects | Narration | Pause | Stop | Voice | Tested |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Code | plugin with HTTP hooks | prompt, answer, tools, permission prompts | yes | yes | yes | yes |
| Pi | TypeScript extension | prompt, answer and thinking as they stream, tools | yes | yes | yes | yes |
| OpenCode | TypeScript plugin | prompt, answer and reasoning as they stream, tools, permission prompts | yes | yes | yes | yes |
| Hermes Agent | Python plugin | prompt, tools, answer, approval prompts | yes | yes | yes | yes |
| Codex CLI | hooks in `~/.codex/hooks.json` | prompt, tools, answer | yes | yes | yes | docs |
| Gemini CLI | hooks in `~/.gemini/settings.json` | prompt, tools, answer | yes | yes | yes | docs |
| Qwen Code | hooks in `~/.qwen/settings.json` | prompt, answer as it streams, tools, permission prompts | yes | yes | yes | docs |
| Copilot CLI | `~/.copilot/hooks/voice-copilot.json` | prompt, tools, permission prompts | yes | yes | yes | docs |
| Grok Build | `~/.grok/hooks/voice-copilot.json` | prompt, tools, answer | yes | yes | yes | docs |
| Droid | hooks in `~/.factory/settings.json` | prompt, tools, answer | yes | yes | yes | docs |
| OpenHands CLI | hooks in `~/.openhands/hooks.json` | prompt, tools, answer | yes | yes | no | docs |
| Kimi CLI | hooks in `~/.kimi/config.toml` | prompt, tools, answer | yes | yes | no | docs |

"Tested" means the integration was run end to end against the real CLI:
narration, pause, stop and a voice message. "Docs" means it follows the CLI's
published hook reference but has not been run here yet; please report what you
see.

Hooks carry no reasoning, so for Claude Code `vc claude` keeps the proxy as
well: the proxy narrates the thinking and the plugin adds permission prompts.
Pi's extension reports everything, so `vc pi` skips the proxy.

## Setting up each CLI

Every CLI below can also be connected with its Install button in the panel's
Plugins tab. `voice-copilot integrate <cli> --print` shows the steps to do it
by hand, and `--uninstall` removes what an install added. JSON settings files
get a one-time backup next to them (`settings.json.voice-copilot.bak`), and
only entries that run `voice-copilot-hook` are added or removed.

### Claude Code

- One session: `vc claude`, or Launch in the panel.
- Every session: `voice-copilot integrate claude`. It writes a local plugin
  marketplace next to the Voice Copilot config and runs `claude plugin
  marketplace add` and `claude plugin install voice-copilot@voice-copilot`.
- By hand, inside Claude Code: `/plugin marketplace add <folder>` and
  `/plugin install voice-copilot@voice-copilot`, where the folder is the one
  `voice-copilot integrate claude --print` prints.
- The installed plugin posts to `127.0.0.1:8765`, the default port of
  `voice-copilot serve`. Use `--port` when you run the panel elsewhere.
- Remove it with `voice-copilot integrate claude --uninstall` or
  `/plugin uninstall voice-copilot@voice-copilot`.

Claude Code waits for the MessageDisplay hook before it draws a line of the
answer. Voice Copilot answers at once, and the plugin caps the wait at five
seconds, so a stuck panel can never freeze the terminal.

### Pi

- One session: `vc pi`, Launch in the panel, or `pi -e <path to voice-copilot.ts>`.
- Every session: `voice-copilot integrate pi` copies the extension to
  `~/.pi/agent/extensions/` (or `$PI_CODING_AGENT_DIR/extensions/`).

### OpenCode

- `voice-copilot integrate opencode` copies the plugin to
  `~/.config/opencode/plugins/`. Restart OpenCode so it loads it.
- For one project only, put `voice-copilot.ts` in `.opencode/plugins/`.

### Hermes Agent

- `voice-copilot integrate hermes` copies the plugin to
  `~/.hermes/plugins/voice_copilot/`.
- Hermes loads shared plugins only when you enable them, in
  `~/.hermes/config.yaml`:

  ```yaml
  plugins:
    enabled: [voice_copilot]
  ```

Hermes 0.19 passes no streamed text or reasoning to plugins, so the answer is
spoken when it is complete.

### Codex CLI

- `voice-copilot integrate codex` adds hooks to `~/.codex/hooks.json`.
- Codex runs a hook only after you trust it: open Codex and approve the new
  hooks in `/hooks`.
- Codex passes no text to hooks until the turn ends, so `vc codex` still
  narrates the answer through the proxy while it streams.

### Gemini CLI, Qwen Code, Droid, OpenHands

`voice-copilot integrate <cli>` merges the hook entries into the CLI's
settings file (see the table). Qwen Code and Gemini CLI may ask you to trust
project hooks; user-level hooks run without asking.

### Copilot CLI and Grok Build

`voice-copilot integrate copilot` writes `~/.copilot/hooks/voice-copilot.json`,
and `voice-copilot integrate grok` writes `~/.grok/hooks/voice-copilot.json`.
Both are files of their own, so removing them never touches your settings.

### Kimi CLI

`voice-copilot integrate kimi` appends `[[hooks]]` tables to
`~/.kimi/config.toml`, between `# >>> voice-copilot hooks >>>` markers so they
can be removed cleanly. Kimi's hooks are still in beta.

### DeepSeek Harness, Oh My Pi and others

DeepSeek Harness runs Claude-format hooks through its `dsh-hooks-claude-code`
bridge, so the Claude entries from `voice-copilot integrate codex --print`
(same format) can be used there by hand. Oh My Pi, Cline, Amp, Cursor CLI,
Crush, Continue and Aider have no integration yet; they are narrated through
the proxy as before.

## How the controls work

Under `voice-copilot serve` the panel's buttons act on the CLI through its
plugin:

- Pause holds the agent at its next tool call until you press resume. The
  model may finish the sentence it is writing; it cannot run anything. A pause
  you forget lets go after 55 minutes, and sooner on a CLI that caps how long
  a hook may take.
- A Supervisor STOP in guard mode refuses every tool call and ends the turn,
  with the reason shown to the model. Typing a new prompt in the terminal, or
  pressing resume, lifts it.
- A voice message goes to the agent at the next tool call it makes (Claude
  Code, Qwen, Gemini, Copilot), at the end of its turn if no tool call comes,
  or at once for plugins that can steer a running turn (Pi, OpenCode, Hermes).

Under `vc`, the terminal wrapper already owns pause and your voice messages
(it suspends the process and types into the terminal), so the plugin only adds
what the proxy cannot see.

A CLI can be narrated through the proxy and report through its plugin at the
same time, for example a globally installed plugin in a terminal whose base
URL points at the proxy. Claude Code, Codex and OpenCode send their session
id with every model request, so the panel sees one session, not two: the
proxy narrates it, and the plugin adds the permission prompts and carries
pause, stop and voice messages to that terminal.

## Environment variables

A launch from `vc` or the panel sets these for the CLI; you rarely set them
yourself.

| Variable | Meaning |
| --- | --- |
| `VOICE_COPILOT_URL` | Where plugins and the forwarder post, default `http://127.0.0.1:8765/api/companion/v1` |
| `VOICE_COPILOT_LAUNCH` | The id of the launch, so reports reach the instance that started the CLI |
| `VOICE_COPILOT_MODE` | `narrate`, or `control` when the proxy already narrates this CLI |
| `VOICE_COPILOT_HOOKS=off` | Silences the forwarder and the Pi, OpenCode and Hermes plugins without removing them |

The hook entries run `voice-copilot-hook`, a small forwarder installed with
Voice Copilot. It exits at once without output when Voice Copilot is not
running, so the CLI works as if the hook were not there. When the command is
not on your `PATH` (a checkout run with `uv run`), `integrate` writes the full
path of the Python interpreter instead.

## For plugin authors

The panel exposes the RFC 0001 companion interface over HTTP at
`http://127.0.0.1:<port>/api/companion/v1`
([RFC](cli-companion-interface.md)):

| Endpoint | Purpose |
| --- | --- |
| `POST /events` | RFC events: `{"cli": "mycli", "session_id": "...", "events": [{"kind": "agent.output", "payload": {"text": "..."}}]}` |
| `POST /gate` | Before a tool runs: answers `{"decision": "allow"}` or `{"decision": "deny", "reason": "...", "stop": true}`, and holds while the user has the agent paused |
| `POST /commands/next` | Long poll for `send_user_message` and `interrupt` commands |
| `POST /commands/result` | Report how a command went |
| `POST /hooks/{claude,gemini,copilot}` | A hook payload in that CLI's own format; the reply is in the same format |
| `GET /status` | Sessions currently reporting |

The Pi, OpenCode and Hermes plugins in `src/voice_copilot/companion/assets/`
are complete examples of the first four.

## Troubleshooting

- The Plugins tab never shows "connected now": check that the panel runs on
  the port the plugin posts to (`curl http://127.0.0.1:8765/api/companion/v1/status`),
  and that the CLI was restarted after the install.
- Codex ignores the hooks: approve them in `/hooks`.
- The same terminal is narrated twice: a CLI routed through the proxy by hand
  (a base-URL variable you set yourself) and also connected through its
  plugin reports everything twice. Use one of the two.
- A plugin for a CLI you launched with `vc` stays silent: that is expected
  for a permanently installed Claude Code plugin, because `vc` loads its own
  copy for the session and the installed one steps aside.

## Security

The panel accepts hook and plugin calls from local processes only. It listens
on `127.0.0.1`, refuses requests that name another host, and refuses POSTs that
a web page sends from another origin, so a site open in your browser cannot
report fake events or hold your agent. Every plugin fails open: when the panel
is unreachable, the CLI carries on as if nothing were installed.
