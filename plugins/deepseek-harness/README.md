# Voice Copilot for DeepSeek Harness

A second model watches what `dsh` does: a cheap one narrates the work aloud, and a stronger
one checks it at a few moments in each task. This plugin connects it to the Voice Copilot
companion, a separate program:

```sh
uv tool install voice-copilot    # or: pipx install voice-copilot
dsh plugin --profile <name> add -w ./plugins/deepseek-harness    # for each profile you use
```

Type `vc off`, `vc on`, `vc status` or `vc check` as the whole message, or `/vc off` and so on.
These never reach the model. Pick a narrator in the panel's Commentator tab: dsh has no way to
run a one-off call without saving a session.

Your messages from the panel reach the agent as real user messages. What it reports to the
companion, at `127.0.0.1` only, is what it sees: your prompts, tool calls with their input and
output, and the model's text and reasoning. See [docs/plugins.md](../../docs/plugins.md).

Tested on dsh 0.2.1-rc.5, a developer preview that warns of breaking changes.
