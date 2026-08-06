# Chat Log Attribution

Tag spans of `.agent/chat_log.md` with task numbers so each task knows which user messages led to it.

## Tag Format

- `<!-- TNNN -->` opens a span (on its own line)
- `<!-- /TNNN -->` closes a span (on its own line)
- Place tags between message `---` separators, not inside messages
- Untagged messages stay unattributed — that's fine
- Pick the primary task if a message relates to multiple

## Process

1. **Find the bookmark**: search chat_log.md for the last `<!-- /TNNN -->` closing tag. Start reading from the line after it. If no tags exist yet, start from the top.

2. **Read a chunk**: read the next ~100 messages (use line offset/limit). Don't try to process the whole file at once.

3. **Identify spans**: for each block of messages, determine which task they relate to. Clues:
   - Explicit task references ("task 042", "work on the judge")
   - `pb-tasks work <N>` / `pb-tasks work done` markers in bash history entries
   - Topic continuity — messages about the same subject belong to the same span
   - General discussion, greetings, or off-topic chat: leave untagged

4. **Insert tags**: edit chat_log.md to add `<!-- TNNN -->` before the first message of a span and `<!-- /TNNN -->` after the last. Place tags on their own lines between `---` separators.

5. **Report**: note which lines you processed and which tasks you attributed. Example: "Processed lines 1-450. Spans: T042 (lines 120-280), T044 (lines 300-420), rest unattributed."

6. **Repeat or stop**: if more unprocessed messages remain, do another chunk. Otherwise, you're done for this session.

## Tips

- Err on the side of not tagging. Wrong attribution is worse than missing attribution.
- Long design discussions before `pb-tasks new` are often the most valuable messages to attribute — they contain the user's raw intent.
- A single message can't belong to two tasks. If it's a transition, close the old span before it and open the new span after it.
- Run `pb-tasks list` first to know which task numbers and names exist.

## Extraction

Once tagged, extract a task's messages with:
```
pb-tasks context <N>
```
