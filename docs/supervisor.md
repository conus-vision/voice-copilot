# Supervisor

Two models, two jobs. The **narrator** is cheap and talks every few seconds:
what the agent is doing right now. The **supervisor** is the strongest model
your CLI can run, and it speaks only at checkpoints — and only when something
is off.

That split is the point. Reviewing an agent's work with a capable model on
every event would be slow and expensive. Reviewing it on a compressed transcript
at a handful of moments per task costs a fraction of one agent turn, and it is
exactly where a stronger model earns its keep: noticing that the worker is
editing the wrong files, looping, or calling something done that isn't.

## Modes

| Setting        | What happens                                                                     |
| -------------- | -------------------------------------------------------------------------------- |
| `off`          | Nothing. Narrator only.                                                          |
| `watch`        | **Supervisor** — reviews checkpoints; a WARN or STOP is spoken to you.           |
| `guard`        | **Supervisor+** — as above, and on STOP it pauses the agent and waits for you.   |

Set it under **Settings → Narration detail → Supervisor**, or per CLI in the
**Per-CLI override** table (a row can turn the supervisor on for `codex` only,
say, without touching the narrator).

## Checkpoints

The supervisor looks when one of these arrives:

- the agent's turn actually ends — not after every tool round: the proxy marks
  a `turn.ended` that ends in tool calls as non-final, so neither narrator nor
  supervisor announces completion mid-task;
- `every_n_tools` tool calls since the last review (default 8);
- the same tool failing twice in a row.

Each review sees the user's goal, the running summary and the last ~80 formatted
events — the whole turn, not just the latest batch.

## Verdicts

The model answers with a first line of `OK`, `WARN` or `STOP`, then one or two
sentences. The parser tolerates `**STOP**` and `STOP:`; anything it cannot read
counts as `OK`, so an unreadable reply never stops the agent.

- `OK` — a quiet row in the Trace, nothing spoken.
- `WARN` — spoken with a "Heads up:" lead-in; the agent keeps going.
- `STOP` in `watch` — spoken like a warning.
- `STOP` in `guard` — the agent is paused, you hear "I paused the agent." and
  the reason, and the panel shows a **Resume agent** banner (or press the
  pause hotkey, `Alt+P`).

## Models

With **Pick models automatically** on, Voice Copilot chooses the weakest model
the CLI offers for the narrator and the strongest for the supervisor:

- **Codex** reads the account's own catalog (`$CODEX_HOME/models_cache.json`,
  refreshed by Codex on every start), ranked by the catalog's priority: the top
  entry supervises. For the narrator the cheapest *general* model wins — a
  `mini` tier if the account has one — because the code-tuned fast tier at the
  very bottom echoes event labels instead of writing prose. Hidden and
  special-purpose entries are skipped.
- **Claude Code** has no local catalog; it uses the CLI's `haiku` / `opus`
  aliases.
- Other CLIs use the static tiers in `commentator/cli_profiles.py`.

An explicit model — global or per CLI — always wins over the automatic pick.
Without the checkbox the supervisor defaults to the CLI's strong tier
(`gpt-5.5` for Codex, `sonnet` for Claude Code).

## What the supervisor does not see

- **The CLI talking to itself.** Codex asks the model for a session title over
  the same connection right after your question; its reply and its turn end
  are tagged `internal` by the proxy and never reach the narrator or the
  supervisor (the Trace folds them under *service messages*).
- **Sub-agents.** A forked helper's final answer is tagged `subagent`; it is
  narrated as "sub-agent finished" and is not a checkpoint. Before this, the
  supervisor reviewed a task that was still running and warned about a job
  "finished without edits".
- **Intermediate turn ends.** A model response that ends in tool calls is
  `final: false` — not the end of the turn.

## Repeated verdicts

A verdict that restates the last spoken one (same status, substantially the
same words) goes to the Trace only, so the supervisor does not nag at every
checkpoint. The exception is `STOP` in `guard` mode, which always pauses the
agent — safety beats de-duplication. Reviews for one session never overlap;
a checkpoint that arrives while a review is in flight is skipped.

## Cost

One supervisor call per checkpoint, on a transcript of at most ~80 short lines.
For a typical task that is a handful of calls to the strong model against
dozens of narrator calls to the cheap one — and against the agent's own turns,
which carry the full context every time.
