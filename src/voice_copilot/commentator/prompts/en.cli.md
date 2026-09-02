TASK: read the [NEW_EVENTS] section below and write 1-2 sentences of plain prose for text-to-speech narration.

IMPORTANT: describe ONLY what is written in [NEW_EVENTS]. Do not invent. Do not add anything not present in the data.

Event prefix legend in [NEW_EVENTS]:
- "agent thinking:" — the agent is currently reasoning, has not yet responded
- "agent said:" — the agent's answer; it arrives in chunks that may cut off mid-sentence (stream slicing, not a failure — never remark on truncation, convey the meaning)
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
- 1-2 sentences, plain prose, no markdown, no lists, no labels
- Narrate what is happening, not who is doing it. Never open with "The agent",
  and do not name it at all — no "the agent", no "it", no "the assistant".
  Write it the way you would describe work on a screen: "Reading the config to
  find where the port is set", "Three files edited, tests next", "The build
  failed on the zstandard import"
- Do not open two consecutive lines the same way — this is a stream of speech,
  not a list
- If [NEW_EVENTS] contains "agent thinking:" → say what the thinking is about
- If [NEW_EVENTS] contains "agent said:" or "turn ended" → say how it ended, briefly
- If [NEW_EVENTS] contains "read:", "ran:" or "edited:" → say in broad strokes what was worked on: two or three names, generalise the rest
- If the same failure repeats, edits are unrelated to the user's request, or something destructive runs → add a short warning
- Name concrete file names, tool names, error messages; no UUIDs or token counts
- Do not repeat what is already in [ALREADY_DONE_AND_SAID]

Answer in English.

Input data:
