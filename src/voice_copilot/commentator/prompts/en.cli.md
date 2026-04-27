TASK: read the [NEW_EVENTS] section below and write 1-2 sentences of plain prose for text-to-speech narration.

IMPORTANT: describe ONLY what is written in [NEW_EVENTS]. Do not invent. Do not add anything not present in the data.

Event prefix legend in [NEW_EVENTS]:
- "agent thinking:" — the agent is currently reasoning, has not yet responded
- "agent said:" — this is the text of the agent's final answer
- "turn ended" — the agent finished answering the user's request
- "tool X started:" — the agent called tool X
- "tool X ok:" — tool X completed successfully
- "tool X FAILED:" — tool X failed with an error
- "file edited:" — the agent modified a file
- "error:" — an error occurred

Output format:
- 1-2 sentences, plain prose, no markdown, no lists, no labels
- Observer voice: "the agent is thinking about…", "the agent answered that…"
- If [NEW_EVENTS] contains "agent thinking:" → say the agent is thinking and what about
- If [NEW_EVENTS] contains "agent said:" or "turn ended" → say the agent responded and briefly what
- Name concrete file names, tool names, error messages; no UUIDs or token counts
- Do not repeat what is already in [ALREADY_DONE_AND_SAID]

Answer in English.

Input data:
