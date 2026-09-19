# Curriculum session adapter (v2)

Load this only when continuing a registered curriculum, not for one-shot teaching. It is the teacher's small interface; the scheduling engine and isolated helper own the rulebook/state. Do not load the generator skill, central state, full course catalog, old controller archive, or scheduling protocol into ordinary lesson context.

## Before teaching

1. Read the project's short `Learning.md` for vault/project IDs. Capture the **parent teacher's** current `PI_SESSION_ID` using bash (print only that variable, not the environment). If unavailable, ask for a stable session identity rather than using a scheduler child's ID. The existing subagent implementation must be installed; if `learning-scheduler` is not discoverable after `/reload`, pause to repair setup, not silently fall back to guessed scheduling.
2. Spawn a **fresh** `learning-scheduler` subagent (standalone context) with absolute vault root, project ID, operation `start/check`, and parent session ID. Wait for its delivered result; do not poll. Its result is the scheduling handoff. An explicit curriculum stays scoped; other-course due reviews are advisory.
3. If blocked, resolve only the reported issue. If the handoff selects a prerequisite repair instead of the requested dependent module, that repair is the single module for this session. Do not combine it with the dependent lesson. If nothing is available, stop; a future review does not become today's task.
4. Read **only** the handoff's `Teaching.md`, selected module packet, and existing learner artifact. They define source authority, rubric, exact source scope, and prior evidence. Expand to an explicitly needed prerequisite/source section only. Follow source hashes/commit checks and stop on a material change or OCR ambiguity. Strict course sources constrain the teach skill's researcher: research only the approved source packet, not web alternatives.
5. Have the scheduler `prepare` the task's fresh empty raw log, or recover an unfinished task as instructed. Display its exact `/md-log <path>` command and pause. The learner must confirm the exact footer. Pass that confirmation back for `confirm-log`, then `begin`. Never use `/md-log` on `Learning.md`, artifacts, course notes, or a historical transcript. If the session/branch changed, tell the helper explicitly; do not relink an old log.
6. Begin diagnostics/retrieval only after the helper confirms the current task is active and logging requirements are met. Setup/confirmation is not learner progress.

## Teach one module

Use the existing teach skill unchanged: probe the goal-relevant frontier → clarify goal → source-grounded plan/DAG → approval → motivated node-by-node teaching/checks. Scheduled retrieval uses **unaided generation** first, not recognition quizzes; repair only demonstrated gaps. Keep current-source claims distinct from documentation, inference, and user attestation.

Preserve the curriculum's exit gate: explain; reconstruct/apply/trace; reason through conditions/tradeoffs; handle changed assumptions. The artifact records actual learner evidence, exact source anchors, misconceptions, remaining gaps, and checkpoint—not merely a polished lesson summary. Math keeps the course's definitions, notation, hypotheses, and techniques. Technical defense includes implementation paths, failure modes, alternatives, and claim evidence.

## Checkpoint and finish (before the final teaching summary)

1. Update only the selected `learning-artifacts/<ID>.md` with the actual evidence and resume point. Never manually append to a linked raw log or edit the central scheduler JSON. Old artifact frontmatter, if archived/migrated, is historical rather than schedule authority.
2. Write an outcome JSON under the project's `learning-system/outcomes/` with:
   - active `task_id` from the scheduler;
   - `outcome`: `passed`, `paused`, or `failed`;
   - four `scores` in the curriculum's dimension order;
   - `exit_passed`: true only for the actual independent exit/fresh recheck;
   - `evidence_path`: the module artifact's vault-relative path;
   - concise `summary`;
   - `node` and precise `resume_action` for paused/failed work.
   Use one file per submitted outcome and do not silently rewrite an already committed pass.
3. Spawn a fresh `learning-scheduler` with operation `checkpoint/finish`, absolute root, project ID, parent session ID, and outcome path. It checks the evidence and calls the deterministic transaction. Wait for its real result; fix reported blockers before claiming completion. Never mark a pass from exposure alone or advance an interrupted review.
4. Report the learning result and the helper's next review appointment separately from its current next-task recommendation. No automatically starting the next module. A failed/paused task retains its exact checkpoint; a passed task is cleared even while logger cleanup remains.
5. Keep the raw log linked through this final summary. For completed work, request `/md-unlog`. Once the learner confirms unlink/no current link, ask the helper to `settle-log`. The next module uses a fresh Pi session, even on the same day. There is no daily module limit.

The helper refreshes the actual clock at both boundaries and owns scheduling. The teacher never needs to reconstruct rolling intervals, reconcile completion events, or scan unrelated course controllers.
