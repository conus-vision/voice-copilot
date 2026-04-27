# X Posts

## Post 1

Voice Copilot is a listening-first companion for coding agents.

A separate commentator LLM analyzes what Claude Code, Codex, or another CLI
agent is doing in parallel, summarizes the important reasoning and output, and
lets you intervene by voice when needed.

It is not prompt dictation, not raw TTS for terminal logs, and not direct
playback of hidden model thinking.

You listen first, read the trace when needed, and stop babysitting the terminal.

MIT open-source.

## Post 2

Most voice tools for developers focus on typing less.

Voice Copilot focuses on reading less.

It runs a second lightweight LLM that summarizes what the coding agent is doing
so you can keep context by listening and step in at the right moment.

It is not raw LLM output read aloud.

## Post 3

New idea we are exploring: audio observability for coding agents.

Voice Copilot watches a running CLI agent, uses a separate LLM to summarize the
important reasoning and output, speaks concise updates, and lets you interrupt
by voice.

Main goal: lower cognitive load while the agent works.

## Short thread

1. We built Voice Copilot because we did not want to stare at terminal output the whole time a coding agent was working.

2. Voice Copilot runs a separate commentator LLM in parallel and turns the noisy stream into short spoken updates.

3. It is not prompt dictation, and it is not raw TTS for LLM output.

4. So the primary workflow becomes: listen, keep context, glance at the trace when needed, interrupt at the right moment.

5. The goal is lower cognitive load and better situational awareness around Claude Code, Codex, and similar CLI agents.