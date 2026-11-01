# Plugins and hooks: narrate a CLI from the inside

Most coding CLIs can load a plugin or run hooks. Voice Copilot ships one for
each CLI that allows it, and the CLI then reports what it does from the inside:
your prompt, the answer as it streams, every tool call with its result, and the
moment it waits for your permission. Nothing is rerouted through the proxy, so
the CLI keeps its own login, endpoint and features.

The plugin also carries the controls back. Pause holds the agent at its next
tool call, a Supervisor STOP refuses its tool calls until you take over, and a
message you say by voice reaches the model while it works.

Claude Code, Codex, Pi, oh-my-pi and DeepSeek Harness have full plugins, with the `vc` commands
in their chat: `voice-copilot setup` connects them, and
[plugins.md](plugins.md) covers them. This page is about the others.

The hooks here don't answer `vc` words: a message like `vc off` goes to the agent as text. Use the
panel to control a CLI connected this way, or `vc <cli>` before its hooks are installed, which goes
through the proxy and takes those words.

## Quick start

Connect the CLI once and keep the panel running:

```bash
voice-copilot integrate gemini      # or opencode, hermes, qwen, copilot, grok, droid, openhands, kimi
voice-copilot serve                 # the panel on http://127.0.0.1:8765
gemini                              # use the CLI as usual, in any terminal
```

The Plugins section of the panel's Settings tab has the same Install button for each CLI, and
shows "connected now" after the first prompt. `voice-copilot integrate` with no
argument lists every integration and whether it is installed.

## What each CLI gives

| CLI | How it connects | Narration | Pause | Stop | Voice | Tested |
| --- | --- | --- | --- | --- | --- | --- |
| OpenCode | TypeScript plugin | prompt, answer and reasoning as they stream, tools, permission prompts | yes | yes | yes | yes |
| Hermes Agent | Python plugin | prompt, tools, answer, approval prompts | yes | yes | yes | yes |
| Gemini CLI | hooks in `~/.gemini/settings.json` | prompt, tools, answer | yes | yes | yes | docs |
| Qwen Code | hooks in `~/.qwen/settings.json` | prompt, answer as it streams, tools, permission prompts | yes | yes | yes | docs |
| Copilot CLI | `~/.copilot/hooks/voice-copilot.json` | prompt, tools, permission prompts | yes | yes | yes | docs |
| Grok Build | `~/.grok/hooks/voice-copilot.json` | prompt, tools, answer | yes | yes | yes | docs |
| Droid | hooks in `~/.factory/settings.json` | prompt, tools, answer | yes | yes | yes | docs |
| OpenHands CLI | hooks in `~/.openhands/hooks.json` | prompt, tools, answer | yes | yes | no | docs |
| Kimi Code | hooks in `~/.kimi-code/config.toml` | prompt, tools, answer | yes | yes | at the end of a turn | yes |

"Tested" means the integration was run end to end against the real CLI:
narration, pause, stop and a voice message. "Docs" means it follows the CLI's
published hook reference but has not been run here yet; please report what you
see.

Hooks carry no reasoning, so `vc claude` keeps the proxy as well: the proxy
narrates the thinking, and a plugin loaded for that one session adds permission
prompts.

Copilot CLI, Droid, Gemini CLI, Grok Build, Hermes Agent, Kimi, OpenHands and
Qwen Code send their model traffic past the proxy on a plain launch: to their
vendor, or through a provider setting the launcher cannot guess. Once their
hooks or plugin are installed, `vc` and Launch skip the proxy for them and
narrate the session from those reports. Before that, the Launch tab says what
the proxy misses for each of them.

## Setting up each CLI

Every CLI below can also be connected with its Install button in the Plugins
section of the panel's Settings tab. `voice-copilot integrate <cli> --print` shows the steps to do it
by hand, and `--uninstall` removes what an install added. JSON settings files
get a one-time backup next to them (`settings.json.voice-copilot.bak`), and
only entries that run `voice-copilot-hub-hook` are added or removed.

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

### Gemini CLI, Qwen Code, Droid, OpenHands

`voice-copilot integrate <cli>` merges the hook entries into the CLI's
settings file (see the table). Qwen Code and Gemini CLI may ask you to trust
project hooks; user-level hooks run without asking.

### Copilot CLI and Grok Build

`voice-copilot integrate copilot` writes `~/.copilot/hooks/voice-copilot.json`,
and `voice-copilot integrate grok` writes `~/.grok/hooks/voice-copilot.json`.
Both are files of their own, so removing them never touches your settings.

### Kimi Code

`voice-copilot integrate kimi` appends `[[hooks]]` tables to
`~/.kimi-code/config.toml` (or `$KIMI_CODE_HOME/config.toml`), between
`# >>> voice-copilot hooks >>>` markers so they can be removed cleanly. The
`kimi` command is Kimi Code now; the Python kimi-cli's last release only
installs it. Kimi Code stops waiting for a hook after 10 minutes, so a pause
lets go then. It takes no context from a tool hook, so a voice message reaches
the agent when its turn ends. Tested end to end with Kimi Code 2.1.1.

### Others

Cline, Amp, Cursor CLI and the rest of the launcher have no integration here: `vc <cli>` narrates
them through the proxy, with the same `vc` words.

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
  or at once for plugins that can steer a running turn (OpenCode, Hermes).

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
| `VOICE_COPILOT_HOOKS=off` | Silences the forwarder and the OpenCode and Hermes plugins without removing them |
| `VOICE_COPILOT_TOKEN` | The panel token, if you set one (see Security); the forwarder and the plugins send it |

The hook entries run `voice-copilot-hub-hook`, a small forwarder installed with
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

The OpenCode and Hermes plugins in `src/voice_copilot/companion/assets/`
are complete examples of the first four.

## Troubleshooting

- The Plugins section never shows "connected now": check that the panel runs on
  the port the plugin posts to (`curl http://127.0.0.1:8765/api/companion/v1/status`),
  and that the CLI was restarted after the install.
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

When the panel listens on a network address (`VOICE_COPILOT_HOST=0.0.0.0`),
requests from other devices must carry its token; calls from this computer,
the hooks and plugins included, still need none. On a machine shared with
other people, set `VOICE_COPILOT_TOKEN` in the environment the CLIs start from:
the panel then asks every caller for it, and the forwarder, the Claude Code
hooks and the OpenCode and Hermes plugins send it.
