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

Set it with `vc review off`, `watch` or `guard` in the chat, under **Settings → Narration
detail → Supervisor**, or per CLI in the **Per-CLI override** table (a row can turn the
supervisor on for `codex` only, say, without touching the narrator).

## Checkpoints

The supervisor looks when one of these arrives:

- the agent's turn actually ends — not after every tool round: the proxy marks
  a `turn.ended` that ends in tool calls as non-final, so neither narrator nor
  supervisor announces completion mid-task;
- `every_n_tools` tool calls since the last review (default 8);
- the same failure coming back: same tool, same error apart from numbers, whatever
  happened in between. Each recurrence is reviewed once. Different errors don't count:
  every shell command reports the tool `Bash`, and a git error, a missing module and a
  failed test aren't one failure;
- you ask: `vc check` in the chat, the **?** button or `Alt+Shift+C`. The others need an
  event, and an agent stuck inside a tool sends none, so this is how you get a review of
  one. See [plugins.md](plugins.md).

Each review sees the user's goal with anything they added while the agent worked, the
running summary, the time and counts so far, the last warning you heard and the last
~80 formatted events — the whole turn, not just the latest batch. It also sees the
latest six edits themselves, cut to 1,200 characters each, so it judges the code and
not just what the agent says about it: a hard-coded special case or a swallowed error
gets caught even when the tests pass. `vc access events` takes the edits away, and
`vc access files` adds the last three files the agent changed: see
[plugins.md](plugins.md#what-the-models-may-read).

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

An explicit model — global or per CLI — always wins over the automatic pick. `vc review model`
picks one from the chat.
Without the checkbox the supervisor defaults to the CLI's strong tier
(`gpt-5.5` for Codex, `sonnet` for Claude Code).

A local model judges only as well as it can read. On 31 checkpoints with a known
right answer, `gpt-6-sol` got all of them and `claude sonnet` 29–30. `gemma4:12b` came
close (92–96%, no missed alert, though a few false STOPs), while `qwen3.5:9b` got 65–67%
and missed real trouble, so use it to narrate, not to supervise. Before editing a
supervisor prompt, and after, run `scripts/eval_supervisor.py` on those checkpoints: a
prompt that reads better can judge worse.

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

The model is shown the last warning you heard and asked to answer `OK` unless it has
something new. A verdict that still restates it (same status, substantially the same
words) goes to the Trace only, so the supervisor does not nag at every checkpoint.
The exception is `STOP` in `guard` mode, which always pauses the agent — safety beats
de-duplication. Reviews for one session never overlap; a checkpoint that arrives
while a review is in flight is skipped.

## When a model doesn't answer

A narrator or supervisor call gets 120 s. A timeout or an empty reply shows up in the
panel as an error, and the supervisor still reviews if the narrator was the one that
failed. On Ollama, thinking models run with `reasoning_effort: none`, because otherwise
they spend the budget on reasoning and return no text.

## Cost

One supervisor call per checkpoint, on a transcript of at most ~80 short lines.
For a typical task that is a handful of calls to the strong model against
dozens of narrator calls to the cheap one — and against the agent's own turns,
which carry the full context every time.

Measured per call through each CLI's own login, on a real task:

| Call | Claude Code | Codex |
|---|---|---|
| Narrator | Haiku: 2.6k tokens in, about $0.0005 | `gpt-5.6-luna`: 16k in, 10k of them cached |
| Supervisor | Sonnet: 4.5k in, about $0.018 | `gpt-6-sol`: 19k in, 15k of them cached |

Each narrated line also takes a summary call of about the same size. Claude's narrator
runs with no tools and no MCP servers, which cut a call from 28k tokens. A Codex call
stays at 9k or more whatever the flags.
