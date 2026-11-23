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

Four ways of working, picked with `vc mode` in the chat, under **Settings → Narration
detail → Mode**, or per CLI in the **Per-CLI override** table (a row can turn
Supervisor+ on for `codex` only, say, without touching the others).

| `vc mode`       | What happens                                                                              |
| --------------- | ----------------------------------------------------------------------------------------- |
| `narrator`      | **Narrator** only: it tells what the agent does, and nobody reviews the work.             |
| `supervisor`    | **Supervisor**: the narrator, plus a stronger model that reviews checkpoints. A WARN or STOP is spoken to you. |
| `plus`          | **Supervisor+** alone: no narration. It checks every command before it runs, reviews checkpoints and speaks only when something matters. |
| `plus-narrator` | **Supervisor+ with the narrator**: both of the above.                                     |

In the config these are two settings, `commentator.narrator` and
`commentator.supervisor.mode` (`off`, `watch` or `plus`). `vc review off|watch|plus` still
sets the second alone. `guard`, Supervisor+'s old name, is read as `plus` wherever it turns
up: an old config, a per-CLI row, `vc review guard`.

The narrator is never off with no supervisor: that would be silence.

## Supervisor+

Supervisor+ is the Supervisor with three more things.

**It checks every command before it runs.** A fixed set of rules, no model, so the answer
takes milliseconds and is the same every time: a recursive delete outside the project's own
folders, `git reset --hard` and other ways to throw away uncommitted work, `git clean -f`, a
force push, deleting a branch, `--no-verify`, rewriting history, `DROP`/`TRUNCATE` through a
database client, a script piped from the internet into a shell, `chmod -R 777`, writing to
`.env`, keys or `~/.ssh`, publishing a package, writing to a whole disk. Shell wrappers
(`bash -lc "..."`, `pwsh -Command ...`, `cmd /c ...`) are looked inside; a command quoted in
a search (`grep "git push --force"`) is text and doesn't fire. So do the forms an agent uses
every day: `rm -rf node_modules`, `rm -rf build`, `git push --force-with-lease`, a folder in
the temp dir. `tests/unit/test_danger.py` is the table of what fires and what must not.

It is a seatbelt for the obvious cases, not a sandbox: anything the rules don't name goes
through, and the CLI's own permissions still apply. Voice Copilot only ever refuses a call;
one it lets go goes on to the CLI's own permission check.

Where the command can be stopped before it runs depends on the route:

| Route | What Supervisor+ can do |
|---|---|
| Plugins: Claude Code, Codex, Pi, oh-my-pi, DeepSeek Harness | Hold it before it runs, as `react` says |
| Hooks from `voice-copilot integrate`, the proxy, a CLI started by `vc` | Only call it out, with the alarm: the call is seen as the model asks for it, or as it starts |

**It reacts the way you choose**, to a dangerous command and to a STOP verdict alike
(`vc react`, or **Settings → Supervisor+ on a STOP or a dangerous command**):

| `vc react` | Dangerous command (plugins) | STOP verdict |
|---|---|---|
| `hold` (default) | Waits for your answer. No answer in 540 s refuses it, before the CLI would kill the hook and let it through. | Pauses the agent until you resume it. |
| `pause` | Waits `pause_s` seconds (30 by default), then lets it go unless you refused. | Pauses the agent for `pause_s` seconds. |
| `say` | Only the alarm and a spoken line; it runs. | Spoken, like the Supervisor's. |

You answer a held command with **Run it** or **Refuse** in the panel's banner, or with
`vc go` / `vc deny` from a terminal. The CLI that is held can't take a chat word until its
tool call is answered, so the panel is the way that always works: if no panel is open when
a command is held, Voice Copilot opens one, and a panel opened later shows what still
waits. Parallel calls are held one by one, oldest first, and a sub-agent's call or one from
a session you aren't watching is held too. `vc off` for one session lets go of that
session's held commands only.

The agent can't answer for you. Under Supervisor+ the words that let a held command go or
loosen the watch (`vc go`, `vc react`, `vc mode`, `vc review`, `vc off`) are refused when
the agent runs them itself, through a shell wrapper or a request to the companion; the
panel shows that it tried. `vc check`, `vc ask` and `vc deny` stay the agent's to use.

The agent is told what happened in plain words: Supervisor+ held the call, and the user
declined it (or didn't answer in time). The narrator and the supervisor read the same in
their log, in the order it happened, and a refused edit leaves the supervisor's RECENT
EDITS and the count of files edited. Measured on Claude Sonnet with the supervisor prompt
as it is: a force push the user allowed gets OK at the end of the turn, and "Done, pushed"
after the user refused it gets a STOP.

The review numbers above (42–46 words) are from Claude Sonnet; with `auto_tier_models`
the supervisor, the advisor and the review run on the CLI's strongest model, `opus` for
Claude Code.

**It has its own sounds**, played in the panel, one per meaning. The Narrator and the
Supervisor modes stay as they were, without them:

| Sound | When |
|---|---|
| two tones down | a WARN |
| three short low pulses | a STOP, or a dangerous command. It cuts the line being read, and it plays even when the voice is muted |
| a rising arpeggio | the agent's turn ended after real work (tool calls), and the verdict, if any, was OK |
| a soft rising pair | a note from the advisor |

The end-of-work sound waits for the turn-end verdict, so "done" is never followed by a
warning. Out of focus the narrator stays quiet (`focus.narrate_only_when_focused`), but
Supervisor+'s warnings and alarms are still spoken, and heard whichever session they are
about. The browser starts sound only after the page has been clicked once: until then the
panel shows its "click to enable audio" button, as it does for speech.

## When the work is done

When the agent's turn ends after real work (tool calls), Supervisor+ closes it with a short
spoken review instead of the narrator's last line, so the end of the work has one voice:
the chime (if the verdict was OK), then two to four sentences on what was done against what
was asked, how it was checked, what is still open, and at most one next step. A question
answered without tools still gets the narrator's answer. **Settings → Supervisor+ reviews
the work out loud** turns it off; the verdict and the chime stay.

The review reads the **case file**, which the code keeps from the events rather than a
model: the checks that ran (tests, type check, lint, build) and how they ended, whether the
code changed after the last one that passed, what Supervisor+ held and what the user said
about it, what the user was already told, and the advisor's notes. So "the tests pass" is
only said when a test passed after the latest edit. Measured on Claude Sonnet in Russian:
42–46 words a review; it said so when the last edit was never checked, and when the agent's
"all 48 tests pass" rested on one test file.

## The advisor

The supervisor asks whether the agent is on track. The advisor asks what the user hasn't:
the security of a login form, data loss in a migration, a dependency added for one line, a
rewrite where a one-line fix would do, an assumption nobody checked. It has no fixed list of
questions. Each time it is asked, it keeps a watch list of two to five things that can go
wrong in *this* task, worked out from the request and the code, and raises a note only when
the events show the agent getting near one of them. A date-format fix gets no security
questions; a sign-up form does.

It looks twice at most, not at every checkpoint: when the agent starts changing files (the
plan is clear by then), and when it edits a sensitive file (auth, payments, migrations,
CI, dependency manifests, settings), at most once a minute. A note it has already raised,
in any words, isn't raised again, and the supervisor's own territory (loops, failing tests,
"done" without checking, destructive commands) is left to the supervisor.

A note comes with the soft chime and waits in the panel, not spoken while the agent works:
what the advisor noticed, and one instruction for the agent. **Send to agent** (or `vc send`,
for the latest one) passes the instruction on as your own message; **Dismiss** puts it
away. `vc advice auto` sends each one at once, `vc advice off` turns the advisor off. The
review at the end mentions the notes still open. Under Supervisor+ the agent can't run
`vc send` or `vc advice` itself.

`scripts/eval_advisor.py` scores the prompt against `scripts/advisor_checkpoints.py`: eight
cases where it should speak up and eight traps (healthy work, a note already raised, the
agent doing it right, the supervisor's job). In Russian: Claude Sonnet 16 of 16; Claude
Opus 15 of 16, no misses and one false alarm (a note on adding reportlab for PDF export);
`gemma4:12b` 13 of 16, one miss and two false alarms, one of them a repeat that the
pipeline drops anyway.

## Another CLI as the supervisor

The supervisor and the narrator can each run through another CLI you are logged in to, on
that CLI's own subscription: Codex supervising Claude Code and the other way round is a
second opinion from another vendor. Set it with `vc review cli codex` (`vc review cli same`
goes back), `vc model cli claude` for the narrator, under **Settings → Supervisor runs
through**, or per CLI in the **Supervisor via** column.

Its model follows that CLI: the automatic pick reads Codex's own catalog when Codex
supervises, and `vc review model` lists that CLI's models. A CLI that isn't installed, or a
model its plan doesn't offer, is an error in the panel, never a quiet switch back.

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
- `STOP` with the Supervisor — spoken like a warning.
- `STOP` with Supervisor+ — what `vc react` says. With `hold` the agent is paused, you
  hear "I paused the agent." and the reason, and the panel shows a **Resume agent**
  banner (or press the pause hotkey, `Alt+P`). With `pause` it goes on by itself after
  `pause_s` seconds unless you take over.

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
picks one from the chat. When the supervisor runs through another CLI, all of this follows
that CLI rather than the one being watched.
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
The exception is `STOP` under Supervisor+ with `hold` or `pause`, which always pauses the
agent — safety beats de-duplication. Reviews for one session never overlap; a checkpoint that arrives
while a review is in flight is skipped.

## When a model doesn't answer

A narrator or supervisor call gets 120 s. A timeout or an empty reply shows up in the
panel as an error, and the supervisor still reviews if the narrator was the one that
failed. On Ollama, thinking models run with `reasoning_effort: none`, because otherwise
they spend the budget on reasoning and return no text.

## A running conversation

Through the Claude CLI the supervisor keeps one conversation per session: one `claude -p`
that stays running, fed review after review. The first message is the one-shot review's,
word for word; each later one carries only what happened since the last verdict ("new
since your last verdict"), and the goal and the earlier events stay in the conversation
above it. The prompt cache then holds everything before the new message.

That is where the saving is. A one-shot `claude -p` marks its whole prompt for the cache
every time, so each review writes 8–9k tokens at the cache-write price and reads back only
the system prompt. Measured on the real message (Opus, a 4.5k-token first review, then
checkpoints of eight tool lines and an edit):

| Review | One-shot | Running conversation |
|---|---|---|
| first | 9.7k written, $0.056 | 7.7k written, $0.046 |
| each next one | 8–9k written, 2k read, about $0.05 | 0.5–0.9k written, 10–13k read, about $0.0055 |
| eight reviews | $0.43 | $0.09 |

At API prices; through a subscription the same tokens count against its limits.

It judges as well. `scripts/eval_supervisor.py --live` splits each checkpoint in two, so
the second half arrives as the new part of a running conversation, and
`scripts/supervisor_sequences.py` adds reviews in a row: a warning followed by nothing new,
quiet reviews and then a force push, the user changing course midway, the same failure at
three checkpoints, "done" with nothing run. On Opus in Russian, one-shot and running alike:
43 of 44 right, one missed alert (the same case both ways), no false alarms, no false STOPs.

It starts over with a full message when the task changes, when the conversation passes
60k tokens, when the model, the language or `vc access` changes, after 50 minutes without a review (Claude Code's
prompt cache is the one-hour kind: a review after 21 idle minutes still read the whole
conversation from it), and after
any review that didn't finish (`vc check` cutting in, a timeout, a dead process): a half
answered turn is never reused, and the error goes to the panel. The process ends with the
session, with `vc off` and with Voice Copilot; if Voice Copilot is killed, the process sees
its input close and exits too.

`vc review memory off` (or **Settings → Keep one running conversation**) goes back to a
fresh call per review. Codex, Gemini and the API providers review one-shot for now. The
advisor and the end-of-work review stay one-shot too: they have their own prompts, and run
two or three times a task.

## Cost

Supervisor+ adds two calls to the supervisor's model a task at most for the advisor, and
one for the review when the work is done. The narrator's closing line is not made then.

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
