# Hacker News Launch Notes

## Title options

- Show HN: Voice Copilot, a listening-first companion for coding agents
- Show HN: Voice Copilot, parallel LLM summaries for coding CLI workflows
- Show HN: Voice Copilot, audio observability and voice control for coding agents

## Launch text

Voice Copilot is an MIT-licensed open-source listening-first companion for coding agents.

A separate commentator LLM runs in parallel to analyze what a coding CLI is
doing, summarize the important parts of the reasoning and response, and speak
concise updates.

It is not prompt dictation and it is not direct TTS for raw LLM output.

The primary workflow is listening-first: hear what matters, read the trace when
needed, and intervene by voice at the right moment.

We built it because we wanted less terminal babysitting and lower cognitive load
while using coding agents like Claude Code and Codex.

## Likely questions to be ready for

1. Why is this better than ordinary TTS?

Because it is summarization, not verbatim playback. The value is compression and
signal, not raw audio output.

2. Why is this better than dictation tools?

Dictation helps you write prompts faster. Voice Copilot helps you understand and
steer a running coding agent with less reading.

3. Why not just read the terminal?

You still can. The point is to reduce constant visual attention and let the user
switch from continuous reading to selective listening plus occasional inspection.

4. Is it tied to one model vendor?

No. The point is to work alongside existing coding CLIs rather than replace them.