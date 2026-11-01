TASK: read the "New events" section below and write one short line of plain prose for text-to-speech narration. The user is busy with something else and listens while the work goes on.

IMPORTANT: describe ONLY what is written in "New events". Do not invent. Do not add anything not present in the data.

Event prefix legend in "New events":
- "agent thinking:" — the agent is currently reasoning, has not yet responded
- "agent said:" — the agent talking as it works; it is its final answer only at the end of the turn. It arrives in chunks that may cut off mid-sentence (stream slicing, not a failure — never remark on truncation, convey the meaning)
- "turn ended" — the agent finished answering the user's request
- "step done, continuing with tools" — an intermediate step; the work goes on
- "sub-agent finished" — one of the agent's forked helpers is done; the main task is still running
- "read:" — files the agent opened
- "searched:" — what the agent looked for
- "ran:" — commands the agent executed
- "edited:" — files the agent changed
- "used X" — the agent called some other tool X
- "tool X FAILED:" — tool X failed with an error
- "file edited:" — the agent modified a file
- "error:" — an error occurred

Output format:
- One sentence, at most 15 words. A second short sentence only for what really matters: an error, a decision, the outcome. This is spoken live, and a long line falls behind the work.
- Plain prose, no markdown, no lists, no labels
- Narrate what is happening, not who is doing it. Never open with "The agent",
  and do not name it at all — no "the agent", no "it", no "the assistant".
  Write it the way you would describe work on a screen: "Reading the config to
  find where the port is set", "Three files edited, tests next", "The build
  failed on the zstandard import"
- Say why, not just what. When the agent names a cause or picks an approach, that is the line
- Do not open two consecutive lines the same way — this is a stream of speech,
  not a list
- If "New events" contains "agent thinking:" → say what the thinking is about
- If it contains "agent said:" before the end of the turn → pass on the cause, decision or finding in it, not the plan
- An edit is not a fix: say "fixed", "works" or "done" only after a check has passed, or at the end of the turn if the agent says so
- At the end of the turn → one sentence with the outcome. If the request was a question, give the answer itself, not "explained". If a check failed or could not run, say so
- At most one file or tool name, without its path. Do not read commands out: say what they do. Skip hiccups already got past in these same events
- If the same failure repeats, edits are unrelated to the user's request, or something destructive runs → add a short warning
- No UUIDs or token counts
- Do not repeat what is in "Already said" or retell the plan. If little is new, a few words will do

Answer in English.

Input data:
