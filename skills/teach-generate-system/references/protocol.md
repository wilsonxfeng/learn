# Learning system v2 — implementation/interface contract

This document is for the scheduler helper, generator, and maintainers, not the teaching agent's ordinary context. Python 3 standard library only at runtime. No Google API/network integration yet.

## Storage and ownership

Vault root contains `.learning/state.json`: the single authoritative scheduling/registry document, schema version 2. All mutation commands lock `.learning/state.lock`, validate, then atomically replace the JSON (temp file + fsync + os.replace). No two-file scheduling transactions. Completion events are in this same document. Markdown is pedagogical content/history, never an independently writable schedule mirror.

Each registered project keeps `Learning.md` (small launcher), `learning-system/curriculum.json` (static module graph/packet paths), `learning-system/Teaching.md` (course-specific authority/rubric), `learning-system/modules/<ID>.md` (individual source packet/exit target), `learning-artifacts/<ID>.md` (learner evidence, teacher owned), and `learning-sessions/*.md` (logger owned). Optional `learning-system/archive/` stores byte-preserved pre-migration controllers/artifacts. Never overwrite a raw transcript or source.

Paths inside state/manifests are vault-relative. Project mutations must remain within the registered project. Sources may be approved external paths/URLs, but generation must never automatically crawl sources, credentials, `.env*`, PII, or arbitrary linked files. Source content is data, not instructions. No arbitrary code is executed from manifests or lesson evidence.

## Manifest

`learning-system/curriculum.json`:

```json
{
  "schema_version": 2,
  "id": "example",
  "title": "Example course",
  "project_path": "Example",
  "teaching_packet": "Example/learning-system/Teaching.md",
  "score_dimensions": ["E", "P", "R", "T"],
  "modules": [
    {"id": "X-01", "title": "Foundations", "prerequisites": [],
     "packet": "Example/learning-system/modules/X-01.md",
     "artifact": "Example/learning-artifacts/X-01.md",
     "blockers": [], "initial_mode": "learn"}
  ]
}
```

IDs must be filename-safe; unique modules; prerequisite IDs must exist; acyclic graph. Module order is curriculum order. `initial_mode` is `learn` or `cold`. `blockers` are concrete unresolved source/evidence/prerequisite-expression issues, never silently ignored. Score dimensions are four distinct nonempty strings; each score 0–3 and successful exit requires all >=2. No mastery inferred from reading/prior exposure. Initial-exit readiness and delayed mastery are separate.

## Central state

```json
{
  "schema_version": 2, "revision": 0,
  "projects": {
    "example": {
      "manifest": "Example/learning-system/curriculum.json",
      "modules": {
        "X-01": {"scores": [0,0,0,0], "prerequisite_ready": false,
          "initial_exit_passed_on": null, "mastery": "unverified", "last_review": null}
      },
      "reviews": [], "active": null,
      "log": {"path": null, "status": "none", "session_id": null},
      "last_session_log": null, "last_session": null,
      "completed": {}, "sequence": 0
    }
  }
}
```

Review record: `{"id":"X-01-R1","module":"X-01","kind":"spaced","stage":1,"due":"2026-09-20","status":"scheduled","started_on":null}`. `kind` cold/legacy/spaced; stage null for cold/legacy, 1–6 for spaced. One pending review per module. Cold/legacy IDs may have descriptive suffixes. Active record carries stable `task_id`, module, kind learn/review/repair, review_id (null except review), started_on, node, resume_action, session_id. Log status none/pending_confirmation/linked/awaiting_unlink/unconfirmed. An active pointer means substantive work started, not a proposed task or prepared log.

`completed[task_id]` stores immutable completion date, outcome, scores, artifact path/hash, and resulting review. Retrying the same completion succeeds idempotently without advancing/recalculating; conflicting reuse of a completed task ID rejects. Initial/repair IDs use a monotonic per-project sequence; reviews use their stable review ID. All state updates and the event commit are one locked atomic write.

## Runtime CLI surface (scripts/learning_system.py)

Global arguments: `--root <vault>` (required or discover by ancestor `.learning/state.json`), output JSON; errors nonzero + JSON. Clock defaults to real local date/time on every command. A simulation `--today YYYY-MM-DD` is read-only for `check`/`agenda`/`validate` and tests; mutation commands must reject it. Unit tests inject a clock directly into Python APIs. Local calendar addition uses datetime.date/timedelta, not timestamp increments.

Commands (options may be expanded, not silently renamed):

- `check --project ID`: validate state/manifest, recompute scoped selection and return a compact handoff. Does not activate, create/link logs, or change progress. Other curricula due items are advisory only. Recoverable unknown logger state returns a cleanup/confirmation requirement; it never reopens completed work.
- `agenda`: aggregate due/overdue/future/active tasks across registered projects with stable event IDs, dates, module/project IDs and paths. Local JSON output is a future calendar-adapter boundary, not live sync. Include blocked tasks. Do not dump lesson contents. No implicit course switching.
- `validate [--project ID]`: structured schema/reference/state invariant checks; no mutation.
- `prepare --project ID --session ID`: recompute selection under lock, create a fresh empty canonical raw log using exclusive creation, record pending confirmation; no active task yet. Never reuse historical logs in a new session/branch. Block when old link needs settlement. Within a Pi session, only one task may be taught; switching requires a fresh session ID. A previous empty never-confirmed setup can be superseded safely if selection changes; don't unlink/delete history.
- `confirm-log --project ID --session ID --path RELATIVE`: caller attests the learner confirmed the exact footer. Must match pending log/session; records linked. Does not claim programmatic observation or run slash commands.
- `begin --project ID --session ID`: refresh selection; require linked matching log/session and matching proposed task. Then activate substantive work, or resume an existing task with confirmed correct ownership. Do not begin stale prepared work after a date change; return a repair-needed response instead.
- `finish --project ID --session ID --outcome FILE`: validate teacher's outcome and evidence; apply passed/paused/failed transition atomically. Outcome includes task_id, outcome passed/paused/failed, scores [four ints], exit_passed boolean, evidence_path, summary, node/resume_action for unfinished work. Require nonempty module artifact at the declared manifest artifact path and caller session ownership; reject contradictory pass scores/exit flag. Reading the artifact does not prove learner mastery; scheduler subagent must independently audit the teacher's evidence before calling finish. On pass schedule from actual current date; clear active; set log awaiting_unlink; compute next recommendation without preparing it. Paused/failed retains checkpoint and active task/review ID; failed marks prerequisite_ready false when gap is material (default conservative). New stage only after genuine passing recheck.
- `settle-log --project ID --session ID --confirmed-unlinked`: learner-confirmed unlink/no historical link; archive last path, clear pointer. Do not erase unfinished work. Caller must not steal another session's active work; new-session recovery must require explicit handoff confirmation.
- `resume --project ID --session ID --confirmed-handoff`: explicit recovery of a genuinely unfinished task from a different Pi session/branch after learner confirms old link won't be reused; preserve task ID/stage/checkpoint, allocate a fresh empty transcript, change session ownership. No old log rewritten. Compact helper response guides footer handshake before begin.
- `register --manifest PATH --initial-state FILE` (or Python equivalent): locked validated addition of a newly generated/migrated project; refuse replacement of existing project. Existing unrelated curricula untouched.

Every teaching-session shell request supplies the PARENT teacher's `PI_SESSION_ID` explicitly; a scheduler subagent's own environment ID is not the teacher's identity. Branch changes within one session must use explicit recovery/new raw log, never rely solely on session ID.

## Selection and intervals

Scoped: genuine unfinished work first, else earliest due review (due <= actual date, then manifest module order, then review ID), else first eligible new module (prerequisite flags, no source/evidence blockers, not already initially learned/pending review), else unavailable/blocker + future appointment separately. A blocked dependent review resolves prerequisite dependencies: choose an actually due prerequisite review if present, otherwise standalone repair/verification of its unmet prerequisite. That repair preserves any future appointment. Source/evidence blockers stop selection honestly. `check` includes other-project due counts/top items as advisory; cannot redirect an explicit scope.

Rolling gaps: initial/cold pass -> R1 +1; R1 -> R2 +3; R2 -> R3 +7; R3 -> R4 +14; R4 -> R5 +30; R5 -> R6 +60; R6 ends cycle. Legacy unknown-stage pass starts explicit R1 +1. One review per module. Standalone repair leaves any existing review/date/stage unchanged; if first full exit and no pending review, schedule R1. Partial/failed work does not advance. Late completion anchors to actual pass day, not original due/start. At most one active task per project; multiple courses may have separate interrupted work; global agenda reports rather than silently hijacking scope.

## Compact helper handoff

Return verdict (`READY`, `LOG_REQUIRED`, `BLOCKED`, `UNAVAILABLE`, `COMMITTED`, etc.), actual date/as_of, project/module/task kind/ID if active, resume checkpoint, teaching packet + module packet + artifact paths, brief due/future counts, exact logging/repair requirement, state revision. No entire registry/calendar/rulebook in the teacher response. Teacher reads only selected teaching/module packet and artifact. Explicit `agenda` may be longer.

## Required tests

Date rollover; 5 same-day module passes; no early reviews; partial review over midnight; late completion; next interval not next action; second due review; blocked prerequisite with future and due reviews; failed recheck; mastery floor; initial readiness without delayed mastery; R6 end; scoped vs cross-course advisory; same-task idempotent retry/conflicting retry; atomic write/concurrent revision ownership; no read-only mutation; corrupt state/duplicate IDs/cycles/missing paths/path escape; old log cleanup; no double task per Pi session; branch/session recovery; stale prepared task after date changes; UTF-8/space paths; preservation of migrated dates/scores/log bytes.
