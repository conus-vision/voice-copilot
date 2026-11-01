TASK: update the session working summary. Return ONLY the new summary text — 2-3 prose sentences, no explanation, no markdown, no labels.

This is internal memory for the commentator; the user never hears it. Goal: keep the thread and avoid repeating yourself in the next narration.

Keep inside:
- What the agent has already done, and where it stopped.
- Concrete files, tools, errors that have come up.
- Key points already narrated to the user — so the next utterance doesn't repeat them.

Only what has happened: no next steps, and nothing counted as done that the agent only said it would do.

Drop trivia. If nothing important changed, copy the previous summary without padding.

Answer in the same language as [JUST_NARRATED_TO_USER].

Input data:
