# Voice Copilot for OpenCode

A second model watches what OpenCode does: a cheap one narrates the work aloud, and a stronger
one checks it at a few moments in each task. This plugin connects it to the Voice Copilot
companion, a separate program:

```sh
uv tool install voice-copilot    # or: pipx install voice-copilot
cp plugins/opencode/voice-copilot.ts ~/.config/opencode/plugins/
```

Type `vc off`, `vc on`, `vc status` or `vc check` as the whole message. These never reach the
model, but OpenCode reports the prompt as failed, as a plugin can stop one only by throwing.
Ignore that error: the answer appears as a toast, or on stderr under `opencode run`.

Your messages from the panel reach the agent as real user messages. What it reports to the
companion, at `127.0.0.1` only, is what it sees: your prompts, tool calls with their input and
output, and the model's text and reasoning. See [docs/plugins.md](../../docs/plugins.md).

Tested on OpenCode 1.19.3.
