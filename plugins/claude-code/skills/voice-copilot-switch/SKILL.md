---
name: voice-copilot-switch
description: Turn Voice Copilot on or off, or say whether it is on. Use when the user asks to enable, disable, mute, stop or resume Voice Copilot, its narration or its supervisor. Not for pausing the agent or for changing what the agent is working on.
---

Run exactly one shell command, with nothing added to it:

- `voice-copilot switch on`
- `voice-copilot switch off`
- `voice-copilot switch status`

Voice Copilot's hook answers this command itself, so it shows up as blocked or denied. That isn't a failure: the reason it gives is the result. Don't retry and don't look for another way. Tell the user the result in one sentence, then go on with whatever else they asked.
