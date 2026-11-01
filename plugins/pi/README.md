# Voice Copilot for Pi

A second model watches what [Pi](https://github.com/earendil-works/pi) does: a cheap one
narrates the work aloud, and a stronger one checks it at a few moments in each task.

This extension connects Pi to the Voice Copilot companion, a separate program:

```sh
uv tool install voice-copilot    # or: pipx install voice-copilot
pi install ./plugins/pi          # from a checkout of this repository
```

Or for one run: `pi -e ./plugins/pi/voice-copilot.ts`. The same file runs on oh-my-pi:
`omp -e ./plugins/pi/voice-copilot.ts`.

Type `vc off`, `vc on`, `vc status` or `vc check` as the whole message. Pi's `input` event
catches these, so the model isn't called.

Your messages from the panel reach Pi as real user messages: as a steer while it works, and
as a new prompt while it's idle. What it reports to the companion, at `127.0.0.1` only, is
what it sees: your prompts, tool calls with their input and output, and the model's text
and reasoning. See [docs/plugins.md](../../docs/plugins.md).
