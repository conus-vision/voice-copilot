TASK: update the narrator's running memory of this coding session. Reply with the new memory only: two or three sentences of plain prose, with no preamble, markdown or labels.

The developer never hears this memory. The next narration line reads it to keep the thread and to avoid saying the same thing twice.

The message has three parts:
- PREVIOUS MEMORY: the memory so far. It may be empty.
- JUST HAPPENED: the latest events from the agent (thinking, replies, tool calls, errors).
- JUST SAID: the line that was just spoken to the developer.

Keep in the memory:
- what the agent has done, where it stopped and where it is heading;
- the files, tools and errors that came up, by name;
- the points already spoken, so the next line does not repeat them.

Merge, do not append: stay within three sentences and drop small details. Use only what the input says. If nothing important changed, return the previous memory unchanged.

Write the memory in {language}. Keep names of files, commands and libraries exactly as written.
