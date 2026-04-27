# Reddit Post

## Draft post

We have been experimenting with a different interface for coding agents.

Voice Copilot is a listening-first companion for coding agents. It runs a
separate commentator LLM next to a coding CLI, analyzes what the agent is doing
in parallel, summarizes the important parts of the reasoning and response, and
speaks short updates.

It is not prompt dictation and it is not direct TTS reading of raw LLM output.

The workflow is meant to be listening-first:

- listen to what matters
- read the trace only when needed
- interrupt by voice when the agent needs correction

The main value we see is lower cognitive load. We do not want to babysit the
terminal constantly while Claude Code, Codex, or another agent is working.

Current direction:

- MIT open-source core
- browser control surface today

What we want feedback on:

- does the listening-first idea resonate?
- is the distinction from dictation clear enough?
- what would make this useful in your real workflow?

## Suggested title options

- Voice Copilot: listening-first companion for coding agents
- We built a tool that narrates coding agents so you do not have to stare at the terminal
- Voice Copilot: parallel LLM summaries for Claude Code, Codex, and other CLI agents