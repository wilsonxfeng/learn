# learn

[![video](assets/thumbnail.png)](https://www.youtube.com/watch?v=kzcI5F4tGiU)

My AI learning system from this video: [How I Use AI to Learn Things](https://www.youtube.com/watch?v=kzcI5F4tGiU).

This is a personal system I built for myself, shared as-is. Built as a pi configuration: the teaching philosophy encoded in a skill, a few small extensions, and agent definitions.

## What's in it

- `skills/teach/` — the philosophy and process; one-shot teaching stays lightweight
- `skills/teach-generate-system/` — opt-in curriculum generator, deterministic scheduler, migration tooling, and tests
- `skills/teach-agenda/` — opt-in cross-curriculum agenda
- `agents/learning-scheduler.md` — isolated scheduler/checker; returns a small handoff instead of loading scheduling rules into the teacher
- `skills/visualize/` — adds a correct, minimal diagram to a lesson when an idea is clearer as a picture
- `extensions/ask-user-question/` — the agent asks you questions through a UI popup
- `extensions/quiz/` — graded questions with instant feedback (✓/✗, correct answer, explanation)
- `extensions/md-log/` — link a markdown file to the session
- `extensions/visual-tools/` — tools for visualization subagents
- `agents/` — `researcher`, `svg-maker`, `mermaid-maker`: the subagents the system delegates to

## Install


This repo **is** a `.pi` directory. From your learning project's root:

```bash
git clone https://github.com/amosblomqvist/learn .pi
```

Then open pi in that directory. (Or copy the pieces you want into your existing project config.)

## Requirements

- [pi](https://github.com/earendil-works/pi)
- A subagent implementation, so the system can spawn the researcher and the visual makers. Recommended: [pi-interactive-subagents](https://github.com/amosblomqvist/pi-interactive-subagents) (tmux only). With it, everything works out of the box. Any other implementation works too, but expect to adapt the agent definitions, e.g. `agents/researcher.md` lists `safe_bash` in its tools, which is specific to that extension.
- `ask-user-question` — use the copy bundled here. If your setup already has an `ask-user-question` extension, use **this** one in its place. Popups from different extensions serialize through a shared UI lock, which only works when it's the same implementation.

## Reusable curricula (learning system v2)

Launch Pi from the workspace root containing this `.pi` directory. After adding/updating the skills, use `/reload` (or restart) to discover the new commands and agent.

```text
/skill:teach-generate-system Create a curriculum for <topic> using <approved source paths>.
/skill:teach Continue <project>/Learning.md.
/skill:teach-agenda
```

Generation asks for missing source/scope decisions, inventories approved material, presents a module/source plan for approval, then creates standardized files. It does **not** start a lesson or infer mastery from prior exposure. Both new command skills are explicitly invoked and hidden from the ordinary model skill list; their bodies load only on invocation.

```text
<workspace>/
  .pi/                              # reusable code/skills, not your course state
  .learning/state.json              # central registry, reviews, active work, completion events
  <project>/
    Learning.md                     # small entry point
    learning-system/
      curriculum.json               # static DAG and packet paths
      Teaching.md                   # source authority and rubric
      modules/<ID>.md               # one module's source/exit packet
      outcomes/                     # teacher-to-scheduler evidence handoffs
      archive/                      # preserved migration history, if applicable
    learning-artifacts/<ID>.md       # curated learner evidence
    learning-sessions/*.md           # raw md-log transcripts; never rewritten by scheduler
```

The teacher reads the small session adapter and selected module only. The `learning-scheduler` subagent runs Python code to check the live date, validate state, choose work, and commit completion. Dates and progress live in the central JSON, not in duplicated Markdown instructions. Scoped course requests stay scoped; other courses' reviews are advisory. Multiple modules per day are supported in separate teaching sessions.

The scheduler code uses Python 3 standard library facilities (POSIX file locking on macOS/Linux). Existing-controller migration additionally uses Ruby's standard YAML parser; ordinary generated systems do not depend on Ruby. See `skills/teach-generate-system/references/generation-spec.md` for the approved input format and generator/migration commands, and `references/protocol.md` there for maintainer details. Tests live under `skills/teach-generate-system/tests/`.

No always-loaded scheduler extension, background reminders, or Google Calendar API calls are added. The agenda's structured local output is the future calendar integration boundary. Normal one-shot `/skill:teach` does not require a registry or scheduler.

## Notes

You can run one-shot teaching without subagents. The main session does the teaching; researcher verification and generated visuals then need an alternative workflow. The v2 curriculum adapter requires the `learning-scheduler` subagent (the underlying CLI remains usable directly for maintenance).

The teaching skill is written for one learner (me). Edit the skill to fit how you learn best.
