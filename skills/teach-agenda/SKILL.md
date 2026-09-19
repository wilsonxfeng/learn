---
name: teach-agenda
description: Show today's central curriculum agenda across registered learning projects, without starting a lesson or loading course content.
disable-model-invocation: true
---

# Central learning agenda

Find the workspace/vault root containing `.learning/state.json`. Spawn a fresh `learning-scheduler` subagent with operation `agenda` and that absolute root. Do not load `/skill:teach`, every controller, the scheduling protocol, or the central JSON into the parent context. Wait for the real result; do not poll.

Present the actual checked date and separate:

1. unfinished tasks;
2. due/overdue reviews (include known blockers);
3. upcoming reviews.

This is a read-only view. Do not activate work, prepare a log, move dates, or switch curricula automatically. If the user asks to begin, let them choose a project and continue its `Learning.md` in a fresh teaching session. Explicitly starting a named project keeps scheduling scoped to it; other projects remain advisory.

The runtime also exposes a local JSON agenda for future calendar integration. Do not claim Google Calendar synchronization, reminders, or background jobs exist.
